#!/usr/bin/env python3
"""A deliberately small OpenID Connect provider, for the SSO live gate.

The unit suite proves the OIDC client against an in-process provider over a
``MockTransport`` — real client code, but no socket. The SSO gate needs the
other half: a provider the *running API process* reaches over TCP, so that
discovery caching, the JWKS fetch, the redirect the browser follows, the token
exchange with PKCE, and the session the edge then accepts are all exercised by
two real processes talking HTTP to each other.

It implements exactly what ARGUS consumes, and it is strict about all of it:

* discovery announces ``issuer`` and is the only document that names the other
  endpoints (so a client that believes a redirected discovery document is
  caught by the issuer check in the API, not papered over here);
* the authorization request must carry ``response_type=code``, a ``state``, a
  ``nonce`` and an S256 ``code_challenge``, and its ``redirect_uri`` must equal
  the registered one byte for byte;
* the token request must authenticate the client (HTTP Basic or a posted
  secret), must present the *same* ``redirect_uri``, must spend the code once,
  and must present the PKCE verifier whose SHA-256 matches the challenge it
  recorded. A wrong verifier is refused — which is what makes a passing login
  evidence that the client stored and sent the right secret.

Counters for every refusal are kept (``/_stats``) so the gate can assert the
API authenticated properly *and* that this provider never had to refuse
anything, rather than inferring it from a 200.

The claims the next login will carry are set through ``POST /_control``: the
gate plays the role of an administrator changing somebody's group membership,
because "a revocation at the provider takes effect at the next login" is a
property worth driving from outside the API.

Usage (from the gate, not by hand):

    python3 infrastructure/lib/oidc-stub.py \
        --issuer http://127.0.0.1:8099 \
        --client-id argus-console \
        --client-secret <secret> \
        --redirect-uri http://localhost:3000/auth/callback
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def int_to_b64(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    return b64url(value.to_bytes(length, "big"))


class Provider:
    """Key material, in-flight codes and the counters the gate asserts on."""

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.kid = "sso-gate-key-1"
        self._private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.lock = threading.Lock()
        #: The claims the *next* authorization will mint. The gate overwrites
        #: these through ``/_control`` to model a change at the provider.
        self.claims: Dict[str, Any] = {
            "sub": "sso-gate-default",
            "email": "sso-gate-default@example.test",
            "email_verified": True,
            "name": "SSO Gate Default",
            "preferred_username": "sso-gate-default",
            "groups": [],
            "argus_projects": [],
        }
        #: ``code`` → the exact request it was issued for. Single use.
        self.codes: Dict[str, Dict[str, Any]] = {}
        self.stats: Dict[str, int] = {
            "authorizations": 0,
            "exchanges": 0,
            "pkce_failures": 0,
            "client_auth_failures": 0,
            "redirect_mismatches": 0,
            "bad_requests": 0,
            "tokens_issued": 0,
        }
        self.authorization_requests: List[Dict[str, str]] = []

    # -- documents ---------------------------------------------------------
    def discovery(self) -> Dict[str, Any]:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "jwks_uri": f"{self.issuer}/jwks",
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "scopes_supported": ["openid", "email", "profile"],
            #: Basic first, so the API's default (and safest) shape is the one
            #: this provider expects.
            "token_endpoint_auth_methods_supported": [
                "client_secret_basic",
                "client_secret_post",
            ],
            #: Advertised on purpose so the client must use S256; the
            #: authorization endpoint refuses a bare ``plain`` challenge.
            "code_challenge_methods_supported": ["S256"],
        }

    def jwks(self) -> Dict[str, Any]:
        numbers = self._private.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "use": "sig",
                    "alg": "RS256",
                    "kid": self.kid,
                    "n": int_to_b64(numbers.n),
                    "e": int_to_b64(numbers.e),
                }
            ]
        }

    def id_token(self, *, nonce: str, claims: Dict[str, Any]) -> str:
        now = int(time.time())
        payload: Dict[str, Any] = {
            "iss": self.issuer,
            "aud": self.client_id,
            "iat": now,
            "exp": now + 300,
            "nonce": nonce,
        }
        payload.update(claims)
        return jwt.encode(
            payload, self._private, algorithm="RS256", headers={"kid": self.kid}
        )

    def snapshot(self) -> Dict[str, Any]:
        return {
            "issuer": self.issuer,
            "stats": dict(self.stats),
            "claims": dict(self.claims),
            "live_codes": len(self.codes),
            "authorization_requests": list(self.authorization_requests),
        }


class Handler(BaseHTTPRequestHandler):
    provider: Provider
    server_version = "argus-oidc-stub/1"

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        sys.stderr.write("oidc-stub: " + (fmt % args) + "\n")

    def _send(self, status: int, payload: Any, *, headers: Optional[dict] = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # -- routes ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's contract
        path = urlparse(self.path).path
        provider = self.provider
        if path == "/health":
            self._send(200, {"status": "ok"})
            return
        if path == "/_stats":
            self._send(200, provider.snapshot())
            return
        if path == "/.well-known/openid-configuration":
            self._send(200, provider.discovery())
            return
        if path == "/jwks":
            self._send(200, provider.jwks())
            return
        if path == "/authorize":
            self._authorize(parse_qs(urlparse(self.path).query))
            return
        self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        provider = self.provider
        if path == "/_control":
            try:
                update = json.loads(self._body().decode("utf-8") or "{}")
            except ValueError:
                self._send(400, {"error": "invalid_json"})
                return
            with provider.lock:
                provider.claims.update(update)
            self._send(200, {"claims": dict(provider.claims)})
            return
        if path == "/token":
            self._token()
            return
        self._send(404, {"error": "not_found"})

    def _authorize(self, query: Dict[str, List[str]]) -> None:
        provider = self.provider

        def one(name: str) -> str:
            values = query.get(name) or [""]
            return values[0]

        redirect_uri = one("redirect_uri")
        if redirect_uri != provider.redirect_uri:
            with provider.lock:
                provider.stats["redirect_mismatches"] += 1
            #: An unregistered redirect is the one error a real provider must
            #: never send the user agent back to, so it is a plain 400 here too.
            self._send(
                400,
                {
                    "error": "invalid_request",
                    "detail": "redirect_uri is not the registered callback",
                },
            )
            return

        problem = ""
        if one("client_id") != provider.client_id:
            problem = "unknown client_id"
        elif one("response_type") != "code":
            problem = "response_type must be code"
        elif one("code_challenge_method") != "S256" or not one("code_challenge"):
            problem = "PKCE with code_challenge_method=S256 is required"
        elif not one("state"):
            problem = "state is required"
        elif not one("nonce"):
            problem = "nonce is required"
        if problem:
            with provider.lock:
                provider.stats["bad_requests"] += 1
            self._send(400, {"error": "invalid_request", "detail": problem})
            return

        state = one("state")
        with provider.lock:
            code = "code_" + secrets.token_urlsafe(24)
            provider.codes[code] = {
                "challenge": one("code_challenge"),
                "redirect_uri": redirect_uri,
                "nonce": one("nonce"),
                "claims": dict(provider.claims),
                "used": False,
            }
            provider.stats["authorizations"] += 1
            provider.authorization_requests.append(
                {
                    "state": state,
                    "nonce": one("nonce"),
                    "scope": one("scope"),
                    "code_challenge": one("code_challenge"),
                    "code_challenge_method": one("code_challenge_method"),
                }
            )
        location = f"{redirect_uri}?{urlencode({'code': code, 'state': state})}"
        self._redirect(location)

    def _token(self) -> None:
        provider = self.provider
        form = parse_qs(self._body().decode("utf-8"))

        def one(name: str) -> str:
            values = form.get(name) or [""]
            return values[0]

        if not self._client_authenticated(form):
            with provider.lock:
                provider.stats["client_auth_failures"] += 1
            self._send(401, {"error": "invalid_client"})
            return

        code = one("code")
        if one("grant_type") != "authorization_code":
            with provider.lock:
                provider.stats["bad_requests"] += 1
            self._send(400, {"error": "unsupported_grant_type"})
            return

        with provider.lock:
            record = provider.codes.get(code)
            if record is None or record["used"]:
                #: A code is single use, exactly like the client's state.
                provider.stats["bad_requests"] += 1
                self._send(400, {"error": "invalid_grant"})
                return
            if one("redirect_uri") != record["redirect_uri"]:
                provider.stats["redirect_mismatches"] += 1
                self._send(400, {"error": "invalid_grant"})
                return
            verifier = one("code_verifier")
            expected = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
            if not verifier or expected != record["challenge"]:
                provider.stats["pkce_failures"] += 1
                self._send(400, {"error": "invalid_grant", "detail": "PKCE mismatch"})
                return
            record["used"] = True

        id_token = provider.id_token(nonce=record["nonce"], claims=record["claims"])
        with provider.lock:
            provider.stats["exchanges"] += 1
            provider.stats["tokens_issued"] += 1
        self._send(
            200,
            {
                "access_token": "opaque-" + secrets.token_urlsafe(12),
                "token_type": "Bearer",
                "expires_in": 300,
                "id_token": id_token,
            },
        )

    def _client_authenticated(self, form: Dict[str, List[str]]) -> bool:
        provider = self.provider
        header = self.headers.get("Authorization") or ""
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return False
            expected = f"{provider.client_id}:{provider.client_secret}"
            return secrets.compare_digest(decoded, expected)
        posted = (form.get("client_secret") or [""])[0]
        return bool(posted) and secrets.compare_digest(posted, provider.client_secret)


def main() -> int:
    parser = argparse.ArgumentParser(description="ARGUS SSO gate stub IdP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    parser.add_argument("--redirect-uri", required=True)
    args = parser.parse_args()

    Handler.provider = Provider(
        issuer=args.issuer,
        client_id=args.client_id,
        client_secret=args.client_secret,
        redirect_uri=args.redirect_uri,
    )
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    sys.stderr.write(f"oidc-stub: listening on http://{args.host}:{args.port}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# Security Policy

## Supported versions

ARGUS is a young project: security fixes are made on the latest `main` and
released in the next tagged version. If you run ARGUS in production, track
`main` or the latest release and apply updates promptly.

| Version | Supported |
| --- | --- |
| latest release / `main` | ✅ |
| older tags | ❌ |

## Reporting a vulnerability

**Do not open a public GitHub issue for security problems.**

Report privately through GitHub's
[private vulnerability reporting](https://github.com/piyush06singhal/argus/security/advisories/new)
or email **piyush.singhal.2004@gmail.com** with `[ARGUS security]` in the subject.

Please include:

- the affected component and, if possible, the affected routes/files,
- reproduction steps or a proof of concept,
- the impact you believe is possible,
- whether the issue is exploitable in the default (Docker Compose) deployment.

You will get an acknowledgement within **72 hours** and a status update at
least every 7 days until the issue is resolved. Credit is given in the release
notes unless you prefer to remain anonymous.

## Security model — what ARGUS promises

ARGUS is designed to run **inside your network**, pointed at your own systems.
Its security posture:

- **Token authentication and roles.** Every non-public route requires a bearer
  token (`ADMIN`, `OPERATOR`, `VIEWER` roles); project-level grants are enforced
  server-side on every request. Unauthenticated access exists only when
  `ARGUS_AUTH_DISABLED=true` and only outside `API_ENVIRONMENT=production`.
- **Ingestion trust is explicit.** OTLP and webhook ingestion require per-source
  ingest tokens; ingestion webhooks additionally verify HMAC signatures with
  timestamp tolerance (replay-resistant).
- **Default-deny remediation.** Remediation actions exist only in a closed
  registry (no shell, ever), pass a five-gate evaluation (safety, policy,
  authorization, approval, freshness) and ship disabled by configuration.
- **No arbitrary code execution from user input.** Fix verification runs
  allowlisted commands in isolated workspaces; failure reproduction runs in
  bounded sandboxes with no host access.
- **Secrets are rejected, not stored.** Payloads that look like credentials are
  refused at ingestion boundaries; the audit log is hash-chained; audit rows are
  never mutated.

## Security model — what ARGUS does *not* promise (yet)

Honest scope, so you can make your own deployment decisions:

- No SSO/OIDC integration yet — API tokens only. Put ARGUS behind your identity
  proxy (or VPN) if you need directory-managed access.
- No TLS termination — run behind your own reverse proxy (nginx, Caddy, traefik).
- Single-region, single-cluster deployments only.
- The web UI trusts the API's authorization for all data access; it holds no
  secrets itself.

These are tracked in `docs/security-architecture.md`.

## Known-good verification

The repository's CI runs lint, typecheck, the full test suite and dependency
audits (`pip-audit`, `npm audit --omit=dev`) on every push. Security-sensitive
behaviour (auth, ingestion trust, remediation bypass attempts, project
isolation) is covered by dedicated test modules — see
`apps/api/tests/test_auth*` and `apps/api/tests/test_phase9_*.py`.

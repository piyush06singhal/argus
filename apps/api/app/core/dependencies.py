"""ARGUS Core Dependencies."""

from __future__ import annotations

import socket
import uuid
from typing import Optional

from fastapi import Depends, Header, Request


def get_client_ip(request: Request) -> Optional[str]:
    """Extract client IP from request."""
    if request.client:
        return request.client.host
    return None


def get_request_id(x_request_id: Optional[str] = Header(None)) -> str:
    """Get or generate a request correlation ID."""
    if x_request_id:
        return x_request_id
    return str(uuid.uuid4())


def get_hostname() -> str:
    """Get the current hostname."""
    return socket.gethostname()


# Authentication-ready dependency (for future use)
async def get_current_org_id(
    x_org_id: Optional[str] = Header(None),
) -> Optional[str]:
    """Get the current organization ID from headers.

    Note: This is a foundation for future multi-tenancy.
    In Phase 0, this returns None (single-tenant mode).
    """
    if x_org_id:
        return x_org_id
    return None


# Authorization-ready dependency (for future use)
async def require_organization(
    org_id: Optional[str] = Depends(get_current_org_id),
) -> str:
    """Require an organization context for a request.

    Note: In Phase 0, single-tenant mode does not enforce an org header.
    This is a placeholder for future access control.
    """
    return org_id or "default"

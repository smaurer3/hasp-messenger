"""Authentication / authorisation.

Trust model:
  * Cloudflare Access sits in front of the app via the tunnel. When a request
    has been Google-authenticated, Cloudflare adds the header
    `Cf-Access-Authenticated-User-Email`. We trust that header — it's only
    ever set by Cloudflare for requests through the tunnel.

  * LAN-direct requests don't go through Cloudflare, so the header is absent.
    These are implicitly trusted as full admins (the LAN bypass).

  * The first ever Cloudflare-authenticated email is auto-promoted to admin
    so the deployment can be bootstrapped with no config file editing.

  * Any subsequent unknown email is auto-added as a *pending* user. They see
    a 'waiting for approval' screen instead of being hard-denied, and admins
    see them flagged in the Users page so they can grant access.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request

from . import config as cfg_module
from . import users as users_store
from .models import User

log = logging.getLogger("hasp.auth")

CF_EMAIL_HEADER = "cf-access-authenticated-user-email"


class ResolvedUser:
    """Effective permissions for the request, with the LAN bypass merged in."""

    __slots__ = ("email", "is_admin", "is_lan", "allowed_plate_ids", "stored")

    def __init__(
        self,
        email: str,
        is_admin: bool,
        is_lan: bool,
        allowed_plate_ids: list[str],
        stored: User | None,
    ) -> None:
        self.email = email
        self.is_admin = is_admin
        self.is_lan = is_lan
        self.allowed_plate_ids = list(allowed_plate_ids)
        self.stored = stored

    def can_access_plate(self, plate_id: str) -> bool:
        return self.is_admin or (plate_id in self.allowed_plate_ids)


def _all_plate_ids() -> list[str]:
    return [p.id for p in cfg_module.load_config().plates]


def resolve_user(request: Request) -> ResolvedUser:
    """Resolve the user for an incoming HTTP / WS request.

    Raises HTTPException(403) when a Cloudflare-authenticated user isn't in
    the users store (and isn't the very first one).
    """
    # Header lookup is case-insensitive in Starlette/FastAPI.
    email = (request.headers.get(CF_EMAIL_HEADER) or "").strip().lower()

    if not email:
        # LAN / direct access — admin bypass with no email tag.
        return ResolvedUser(
            email="",
            is_admin=True,
            is_lan=True,
            allowed_plate_ids=_all_plate_ids(),
            stored=None,
        )

    existing = users_store.get_user(email)
    if existing is not None:
        return ResolvedUser(
            email=existing.email,
            is_admin=existing.is_admin,
            is_lan=False,
            allowed_plate_ids=(
                _all_plate_ids() if existing.is_admin else list(existing.allowed_plate_ids)
            ),
            stored=existing,
        )

    # Unknown email. Auto-promote if the users list is still empty (very first
    # CF login bootstraps as admin). Otherwise, create a pending request — the
    # user lands on a "waiting for approval" screen and admins see them in the
    # Users page.
    if users_store.count_users() == 0:
        bootstrap = User(
            email=email,
            name=email.split("@")[0],
            is_admin=True,
            allowed_plate_ids=_all_plate_ids(),
        )
        users_store.upsert_user(bootstrap)
        log.info("Bootstrapped first CF user as admin: %s", email)
        return ResolvedUser(
            email=bootstrap.email,
            is_admin=True,
            is_lan=False,
            allowed_plate_ids=_all_plate_ids(),
            stored=bootstrap,
        )

    pending = User(
        email=email,
        name=email.split("@")[0],
        is_admin=False,
        allowed_plate_ids=[],
        pending=True,
    )
    users_store.upsert_user(pending)
    log.info("Auto-added pending access request for %s", email)
    return ResolvedUser(
        email=pending.email,
        is_admin=False,
        is_lan=False,
        allowed_plate_ids=[],
        stored=pending,
    )


def require_admin(request: Request) -> ResolvedUser:
    user = resolve_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_plate_access(request: Request, plate_id: str) -> ResolvedUser:
    user = resolve_user(request)
    if not user.can_access_plate(plate_id):
        raise HTTPException(
            status_code=403,
            detail=f"You don't have access to this plate ({plate_id}).",
        )
    return user

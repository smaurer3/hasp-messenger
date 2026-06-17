"""Cloudflare Access policy sync.

Keeps the local users.json in step with a single Cloudflare Access policy's
`include` list. Add / remove a user in the app → email is also added /
removed from the CF policy, so an admin can drive the whole authorisation
chain from this UI without touching the CF dashboard.

Failures are returned via `CloudflareResult` rather than raised, so callers
can decide whether to surface them to the admin or just log them.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from .models import CloudflareConfig

log = logging.getLogger("hasp.cloudflare")

CF_API = "https://api.cloudflare.com/client/v4"


class CloudflareResult:
    __slots__ = ("ok", "detail")

    def __init__(self, ok: bool, detail: str = "") -> None:
        self.ok = ok
        self.detail = detail


def _err_messages(payload: dict) -> str:
    errs = payload.get("errors") or []
    if not errs:
        return "unknown error"
    return "; ".join(f"{e.get('code')}: {e.get('message')}" for e in errs)


async def _request(
    cfg: CloudflareConfig,
    method: str,
    path: str,
    *,
    json_body: Optional[dict] = None,
    timeout: float = 10.0,
) -> tuple[bool, dict]:
    headers = {"Authorization": f"Bearer {cfg.api_token}"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.request(method, f"{CF_API}{path}", headers=headers, json=json_body)
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        return False, {"errors": [{"code": r.status_code, "message": r.text[:200]}]}
    return bool(data.get("success")), data


def _is_configured(cfg: CloudflareConfig) -> bool:
    return bool(
        cfg.enabled
        and cfg.api_token
        and cfg.account_id
        and (cfg.application_uid or cfg.application_name)
        and (cfg.policy_uid or cfg.policy_name)
    )


async def _resolve_ids(cfg: CloudflareConfig) -> CloudflareResult:
    """Look up the app + policy UUIDs from their names, write back to cfg.

    The policy is first looked up under the app (legacy / app-scoped policies).
    If nothing matches there, we fall back to the account-scoped *reusable*
    policy list — CF's newer policy type that can be attached to multiple apps.
    """
    app_uid = cfg.application_uid
    if not app_uid:
        ok, data = await _request(cfg, "GET", f"/accounts/{cfg.account_id}/access/apps")
        if not ok:
            return CloudflareResult(False, f"list apps failed: {_err_messages(data)}")
        wanted = cfg.application_name.lower()
        match = next((a for a in data.get("result", []) if a["name"].lower() == wanted), None)
        if match is None:
            return CloudflareResult(False, f"no app named {cfg.application_name!r}")
        app_uid = match["id"]
        cfg.application_uid = app_uid
    if not cfg.policy_uid:
        wanted = cfg.policy_name.lower()
        # First try the app's own policies (app-scoped, non-reusable).
        ok, data = await _request(
            cfg, "GET", f"/accounts/{cfg.account_id}/access/apps/{app_uid}/policies",
        )
        if not ok:
            cfg.application_uid = ""
            return CloudflareResult(False, f"list policies failed: {_err_messages(data)}")
        match = next((p for p in data.get("result", []) if p["name"].lower() == wanted), None)
        if match is None:
            # Fall back to reusable policies (account-scoped).
            ok, data = await _request(
                cfg, "GET", f"/accounts/{cfg.account_id}/access/policies",
            )
            if not ok:
                return CloudflareResult(False, f"list reusable policies failed: {_err_messages(data)}")
            match = next((p for p in data.get("result", []) if p["name"].lower() == wanted), None)
        if match is None:
            return CloudflareResult(False, f"no policy named {cfg.policy_name!r}")
        cfg.policy_uid = match["id"]
    return CloudflareResult(True)


def _policy_path(cfg: CloudflareConfig, reusable: bool) -> str:
    if reusable:
        return f"/accounts/{cfg.account_id}/access/policies/{cfg.policy_uid}"
    return (
        f"/accounts/{cfg.account_id}/access/apps/{cfg.application_uid}"
        f"/policies/{cfg.policy_uid}"
    )


async def _fetch_policy(cfg: CloudflareConfig) -> tuple[bool, dict | str]:
    """Fetch the policy. Tries app-scoped first; falls back to reusable."""
    # Try app-scoped.
    ok, data = await _request(cfg, "GET", _policy_path(cfg, reusable=False))
    if ok:
        return True, data["result"]
    # Fall back to reusable (account-scoped) endpoint.
    ok, data = await _request(cfg, "GET", _policy_path(cfg, reusable=True))
    if ok:
        return True, data["result"]
    cfg.application_uid = ""
    cfg.policy_uid = ""
    return False, _err_messages(data)


async def _put_policy(cfg: CloudflareConfig, policy: dict) -> tuple[bool, str]:
    # Cloudflare requires every field the policy was created with on PUT;
    # echo back what we just read minus the auto-generated bits.
    reusable = bool(policy.get("reusable"))
    body = {
        k: v for k, v in policy.items()
        if k not in {"id", "created_at", "updated_at", "reusable"}
    }
    ok, data = await _request(cfg, "PUT", _policy_path(cfg, reusable), json_body=body)
    if not ok:
        return False, _err_messages(data)
    return True, ""


def _email_from_rule(rule: dict) -> Optional[str]:
    """Return the lowercase email if this include-rule is a simple email rule."""
    email_block = rule.get("email") if isinstance(rule, dict) else None
    if isinstance(email_block, dict):
        e = email_block.get("email")
        if isinstance(e, str):
            return e.strip().lower()
    return None


async def list_policy_emails(cfg: CloudflareConfig) -> tuple[list[str], Optional[str]]:
    if not _is_configured(cfg):
        return [], "Cloudflare sync is disabled"
    r = await _resolve_ids(cfg)
    if not r.ok:
        return [], r.detail
    ok, policy_or_err = await _fetch_policy(cfg)
    if not ok:
        return [], str(policy_or_err)
    emails = []
    for rule in policy_or_err.get("include") or []:
        e = _email_from_rule(rule)
        if e and e not in emails:
            emails.append(e)
    return emails, None


async def add_user(cfg: CloudflareConfig, email: str) -> CloudflareResult:
    if not _is_configured(cfg):
        return CloudflareResult(True, "(CF sync disabled — local only)")
    email = email.strip().lower()
    r = await _resolve_ids(cfg)
    if not r.ok:
        return r
    ok, policy_or_err = await _fetch_policy(cfg)
    if not ok:
        return CloudflareResult(False, str(policy_or_err))
    policy = policy_or_err
    includes = list(policy.get("include") or [])
    if any(_email_from_rule(rule) == email for rule in includes):
        return CloudflareResult(True, "already present")
    includes.append({"email": {"email": email}})
    policy["include"] = includes
    ok, err = await _put_policy(cfg, policy)
    if not ok:
        return CloudflareResult(False, err)
    log.info("CF: added %s to policy %s", email, cfg.policy_name or cfg.policy_uid)
    return CloudflareResult(True)


async def remove_user(cfg: CloudflareConfig, email: str) -> CloudflareResult:
    if not _is_configured(cfg):
        return CloudflareResult(True, "(CF sync disabled — local only)")
    email = email.strip().lower()
    r = await _resolve_ids(cfg)
    if not r.ok:
        return r
    ok, policy_or_err = await _fetch_policy(cfg)
    if not ok:
        return CloudflareResult(False, str(policy_or_err))
    policy = policy_or_err
    includes = policy.get("include") or []
    before = len(includes)
    includes = [rule for rule in includes if _email_from_rule(rule) != email]
    if len(includes) == before:
        return CloudflareResult(True, "not in policy")
    policy["include"] = includes
    ok, err = await _put_policy(cfg, policy)
    if not ok:
        return CloudflareResult(False, err)
    log.info("CF: removed %s from policy %s", email, cfg.policy_name or cfg.policy_uid)
    return CloudflareResult(True)


async def verify(cfg: CloudflareConfig) -> CloudflareResult:
    """Resolve UIDs and fetch the policy — used by the 'Test' button."""
    if not (cfg.api_token and cfg.account_id):
        return CloudflareResult(False, "API token and account ID required")
    # Clear cached UIDs so a verify after a config change always re-resolves.
    cfg.application_uid = ""
    cfg.policy_uid = ""
    r = await _resolve_ids(cfg)
    if not r.ok:
        return r
    ok, policy_or_err = await _fetch_policy(cfg)
    if not ok:
        return CloudflareResult(False, str(policy_or_err))
    n = sum(1 for rule in policy_or_err.get("include") or [] if _email_from_rule(rule))
    return CloudflareResult(True, f"app+policy resolved, {n} email rule(s) in policy")

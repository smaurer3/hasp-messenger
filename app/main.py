from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import cloudflare, config, storage
from . import users as users_store
from .auth import ResolvedUser, require_admin, resolve_user
from .models import (
    BrokerConfig,
    BrokerConfigPublic,
    CloudflareConfig,
    CloudflareConfigPublic,
    MessageSpec,
    Plate,
    SendRequest,
    Template,
    User,
    UserUpdate,
)
from .mqtt_client import MqttClient
from .ws_manager import WsManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("hasp.app")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class PlateState:
    def __init__(self) -> None:
        self.current_spec: Optional[MessageSpec] = None
        self.active_until: Optional[float] = None
        self.clear_task: Optional[asyncio.Task] = None


class AppState:
    def __init__(self) -> None:
        self.ws = WsManager()
        self.mqtt: Optional[MqttClient] = None
        self.plate_states: dict[str, PlateState] = {}


state = AppState()


def _get_plate(plate_id: str) -> Plate:
    cfg = config.load_config()
    for p in cfg.plates:
        if p.id == plate_id:
            return p
    raise HTTPException(status_code=404, detail="Plate not found")


def _get_plate_state(plate_id: str) -> PlateState:
    ps = state.plate_states.get(plate_id)
    if ps is None:
        ps = PlateState()
        state.plate_states[plate_id] = ps
    return ps


async def broadcast_state(message: dict) -> None:
    await state.ws.broadcast(message)


async def broadcast_display(plate_id: str) -> None:
    ps = state.plate_states.get(plate_id)
    payload = {
        "type": "display_state",
        "plate_id": plate_id,
        "active": ps is not None and ps.current_spec is not None,
        "active_until": ps.active_until if ps else None,
        "spec": ps.current_spec.model_dump() if (ps and ps.current_spec) else None,
    }
    await state.ws.broadcast(payload)


def slugify(s: str) -> str:
    """Lowercase, replace runs of non-alphanumerics with hyphens. Stable so the
    same plate/template name always produces the same MQTT topic segment."""
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


async def trigger_template(plate_slug: str, template_slug: str, body: dict) -> None:
    """Find the plate + template that match the given slugs and send it.

    `body` is a (parsed) dict from the MQTT payload; supports an optional
    `duration_seconds` override. Empty or unparseable payloads are fine.
    """
    plate_slug = (plate_slug or "").lower()
    template_slug = (template_slug or "").lower()
    cfg = config.load_config()
    plate = next(
        (p for p in cfg.plates if slugify(p.name) == plate_slug or p.id == plate_slug),
        None,
    )
    if plate is None:
        log.warning("template trigger: no plate matches slug %r", plate_slug)
        return
    template = next(
        (t for t in storage.list_templates(plate_id=plate.id)
         if slugify(t.name) == template_slug),
        None,
    )
    if template is None:
        log.warning("template trigger: no template %r on plate %s", template_slug, plate.name)
        return
    # Payload override > template's saved duration > no auto-clear.
    duration = body.get("duration_seconds") if isinstance(body, dict) else None
    if duration is None:
        duration = template.duration_seconds
    try:
        await _do_send(plate, SendRequest(spec=template.spec, duration_seconds=duration))
        log.info("template trigger: sent %r to %s (clear in %s)",
                 template.name, plate.name,
                 f"{duration}s" if duration else "manual")
    except HTTPException as he:
        log.warning("template trigger failed: %s", he.detail)


@asynccontextmanager
async def lifespan(_: FastAPI):
    loop = asyncio.get_running_loop()
    cfg = config.load_config()
    # One-time migration: assign templates without a plate_id to the first
    # configured plate, so existing saved messages don't go invisible.
    if cfg.plates:
        migrated = storage.migrate_orphan_templates(cfg.plates[0].id)
        if migrated:
            log.info("migrated %d orphan template(s) to plate %s",
                     migrated, cfg.plates[0].id)
    state.mqtt = MqttClient(cfg, broadcast_state, loop, on_template_trigger=trigger_template)
    state.mqtt.start()
    yield
    for ps in state.plate_states.values():
        if ps.clear_task and not ps.clear_task.done():
            ps.clear_task.cancel()
    if state.mqtt:
        state.mqtt.stop()


app = FastAPI(title="HASP Messenger", lifespan=lifespan)


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    """Make sure browsers and any upstream cache (Cloudflare) always pick up the
    latest frontend. The app is interactive and tiny — caching the HTML/JS
    causes more pain than it saves."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


def _static_build_id() -> str:
    """A version stamp derived from the latest mtime of the served static
    files. Changes whenever any of them is touched, so the `?v=<id>` query
    parameter injected into asset URLs is always fresh — Cloudflare and
    browsers treat the new URL as a different resource and refetch."""
    paths = [STATIC_DIR / n for n in ("index.html", "app.js", "style.css")]
    try:
        return str(int(max(p.stat().st_mtime for p in paths if p.exists())))
    except (ValueError, OSError):
        return "0"


@app.get("/")
async def index():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    build = _static_build_id()
    # Rewrite the placeholder ?v=3 markers in the HTML so every reload sees
    # the latest build id (no manual bumping required).
    html = html.replace("/static/app.js?v=3", f"/static/app.js?v={build}")
    html = html.replace("/static/style.css?v=3", f"/static/style.css?v={build}")
    return HTMLResponse(html)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --- Config ---

def _to_public(cfg: BrokerConfig) -> BrokerConfigPublic:
    cf = cfg.cloudflare
    return BrokerConfigPublic(
        host=cfg.host,
        port=cfg.port,
        username=cfg.username,
        password_set=bool(cfg.password),
        use_tls=cfg.use_tls,
        client_id=cfg.client_id,
        plates=cfg.plates,
        cloudflare=CloudflareConfigPublic(
            enabled=cf.enabled,
            account_id=cf.account_id,
            application_name=cf.application_name,
            policy_name=cf.policy_name,
            application_uid=cf.application_uid,
            policy_uid=cf.policy_uid,
            api_token_set=bool(cf.api_token),
        ),
    )


@app.get("/api/config", response_model=BrokerConfigPublic)
async def get_config(_: ResolvedUser = Depends(require_admin)):
    return _to_public(config.load_config())


class CloudflareUpdate(BaseModel):
    enabled: Optional[bool] = None
    api_token: Optional[str] = None      # only set if non-empty string
    clear_api_token: bool = False
    account_id: Optional[str] = None
    application_name: Optional[str] = None
    policy_name: Optional[str] = None


class ConfigUpdate(BaseModel):
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None
    clear_password: bool = False
    use_tls: Optional[bool] = None
    client_id: Optional[str] = None
    plates: Optional[list[Plate]] = None
    cloudflare: Optional[CloudflareUpdate] = None


@app.post("/api/config", response_model=BrokerConfigPublic)
async def update_config(body: ConfigUpdate, _: ResolvedUser = Depends(require_admin)):
    cfg = config.load_config()
    data = body.model_dump(exclude_unset=True)
    clear_pw = data.pop("clear_password", False)
    pw = data.pop("password", None)
    plates = data.pop("plates", None)
    cf_update = data.pop("cloudflare", None)
    for k, v in data.items():
        setattr(cfg, k, v)
    if clear_pw:
        cfg.password = ""
    elif pw is not None and pw != "":
        cfg.password = pw
    if cf_update is not None:
        cf = cfg.cloudflare
        for k, v in cf_update.items():
            if k in {"api_token", "clear_api_token"}:
                continue
            if v is not None:
                setattr(cf, k, v)
        if cf_update.get("clear_api_token"):
            cf.api_token = ""
        elif cf_update.get("api_token"):
            cf.api_token = cf_update["api_token"]
        # Force re-resolve next call — names may have changed.
        cf.application_uid = ""
        cf.policy_uid = ""
    if plates is not None:
        # Preserve existing ids where a plate position matches; otherwise
        # accept ids the client provided (or generate new). Convert dicts
        # back to Plate models.
        new_plates: list[Plate] = []
        for item in plates:
            if isinstance(item, Plate):
                new_plates.append(item)
            else:
                new_plates.append(Plate.model_validate(item))
        cfg.plates = new_plates
    config.save_config(cfg)
    # Drop per-plate runtime state for plates that no longer exist
    valid_ids = {p.id for p in cfg.plates}
    for stale_id in list(state.plate_states.keys()):
        if stale_id not in valid_ids:
            ps = state.plate_states.pop(stale_id)
            if ps.clear_task and not ps.clear_task.done():
                ps.clear_task.cancel()
    if state.mqtt:
        state.mqtt.update_config(cfg)
    return _to_public(cfg)


# --- Templates ---

@app.get("/api/templates", response_model=list[Template])
async def list_templates_api(
    plate_id: Optional[str] = None,
    user: ResolvedUser = Depends(resolve_user),
):
    if plate_id is not None:
        # If the caller asked for a specific plate, enforce access.
        if not user.can_access_plate(plate_id):
            raise HTTPException(status_code=403, detail="No access to this plate")
        return storage.list_templates(plate_id=plate_id)
    # No plate filter — return only templates whose plate the user can see.
    all_tpls = storage.list_templates()
    return [t for t in all_tpls if t.plate_id is None or user.can_access_plate(t.plate_id)]


@app.post("/api/templates", response_model=Template)
async def create_template_api(tpl: Template, user: ResolvedUser = Depends(resolve_user)):
    if not tpl.plate_id:
        raise HTTPException(status_code=400, detail="plate_id is required")
    if not user.can_access_plate(tpl.plate_id):
        raise HTTPException(status_code=403, detail="No access to this plate")
    return storage.create_template(tpl)


@app.put("/api/templates/{tid}", response_model=Template)
async def update_template_api(tid: str, tpl: Template, _: ResolvedUser = Depends(resolve_user)):
    result = storage.update_template(tid, tpl)
    if result is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return result


@app.delete("/api/templates/{tid}")
async def delete_template_api(tid: str, _: ResolvedUser = Depends(resolve_user)):
    if not storage.delete_template(tid):
        raise HTTPException(status_code=404, detail="Template not found")
    return {"ok": True}


# --- Send / clear / init per plate ---

async def _schedule_clear(plate: Plate, duration: float) -> None:
    try:
        await asyncio.sleep(duration)
        if state.mqtt:
            ok, err = state.mqtt.publish_clear(plate)
            if not ok:
                log.warning("auto-clear failed: %s", err)
        ps = state.plate_states.get(plate.id)
        if ps:
            ps.current_spec = None
            ps.active_until = None
        await broadcast_display(plate.id)
    except asyncio.CancelledError:
        pass


def _cancel_clear(plate_id: str) -> None:
    ps = state.plate_states.get(plate_id)
    if ps and ps.clear_task and not ps.clear_task.done():
        ps.clear_task.cancel()
    if ps:
        ps.clear_task = None


def _resolve_for_plate(plate_id: str, request: Request) -> tuple[Plate, ResolvedUser]:
    user = resolve_user(request)
    plate = _get_plate(plate_id)
    if not user.can_access_plate(plate_id):
        raise HTTPException(status_code=403, detail="You don't have access to this plate")
    return plate, user


async def _do_send(plate: Plate, body: SendRequest) -> None:
    """Core send logic shared by HTTP and WebSocket handlers."""
    if not state.mqtt:
        raise HTTPException(status_code=503, detail="MQTT not initialised")
    payload = body.spec.to_hasp_payload(plate.overlay_id, plate.overlay_page)
    ok, err = state.mqtt.publish_jsonl(plate, payload)
    if not ok:
        raise HTTPException(status_code=502, detail=f"Publish failed: {err}")

    _cancel_clear(plate.id)
    ps = _get_plate_state(plate.id)
    ps.current_spec = body.spec
    if body.duration_seconds and body.duration_seconds > 0:
        ps.active_until = time.time() + body.duration_seconds
        ps.clear_task = asyncio.create_task(_schedule_clear(plate, body.duration_seconds))
    else:
        ps.active_until = None
    await broadcast_display(plate.id)


async def _do_clear(plate: Plate) -> None:
    if not state.mqtt:
        raise HTTPException(status_code=503, detail="MQTT not initialised")
    _cancel_clear(plate.id)
    ok, err = state.mqtt.publish_clear(plate)
    if not ok:
        raise HTTPException(status_code=502, detail=f"Publish failed: {err}")
    ps = _get_plate_state(plate.id)
    ps.current_spec = None
    ps.active_until = None
    await broadcast_display(plate.id)


@app.post("/api/plates/{plate_id}/send")
async def send_api(plate_id: str, body: SendRequest, request: Request):
    plate, _user = _resolve_for_plate(plate_id, request)
    await _do_send(plate, body)
    return {"ok": True}


@app.get("/api/plates/{plate_id}/snapshot")
async def snapshot_api(plate_id: str, request: Request):
    plate, _user = _resolve_for_plate(plate_id, request)
    if not plate.ip_address:
        raise HTTPException(status_code=400, detail="Plate has no IP address set — add one in Setup")
    url = f"http://{plate.ip_address}/screenshot?q=0"
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            r = await client.get(url)
    except httpx.RequestError as e:
        log.warning("snapshot fetch error for %s: %r", url, e)
        raise HTTPException(status_code=502, detail=f"Snapshot fetch failed ({type(e).__name__}): {e}")
    if r.status_code != 200:
        log.warning("snapshot HTTP %s for %s: %s", r.status_code, url, r.text[:200])
        raise HTTPException(
            status_code=502,
            detail=f"Plate returned HTTP {r.status_code} (URL: {url})",
        )
    return Response(
        content=r.content,
        media_type=r.headers.get("Content-Type", "image/bmp"),
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/plates/{plate_id}/init")
async def init_api(plate_id: str, request: Request):
    plate, _user = _resolve_for_plate(plate_id, request)
    if not state.mqtt:
        raise HTTPException(status_code=503, detail="MQTT not initialised")
    ok, err = state.mqtt.publish_init(plate)
    if not ok:
        raise HTTPException(status_code=502, detail=f"Publish failed: {err}")
    return {"ok": True}


@app.post("/api/plates/{plate_id}/clear")
async def clear_api(plate_id: str, request: Request):
    plate, _user = _resolve_for_plate(plate_id, request)
    await _do_clear(plate)
    return {"ok": True}


# --- Me / Users ---

@app.get("/api/debug/whoami")
async def debug_whoami_api(request: Request):
    """Echo what the server actually sees about the incoming request.

    Useful when diagnosing 'why am I LAN admin instead of my email?' issues.
    Returns just CF/auth-relevant headers (not cookies) and the resolved
    user. Anyone who can reach the app can call this; that's intentional
    because the symptom we're debugging is *what* identity the request has.
    """
    headers_of_interest = {
        k: v for k, v in request.headers.items()
        if k.lower().startswith("cf-") or k.lower() in {
            "host", "x-forwarded-for", "x-forwarded-host", "x-real-ip", "user-agent"
        }
    }
    try:
        user = resolve_user(request)
        resolved = {
            "email": user.email, "is_admin": user.is_admin,
            "is_lan": user.is_lan, "allowed_plate_ids": user.allowed_plate_ids,
        }
        denied = None
    except HTTPException as he:
        resolved = None
        denied = he.detail
    return {
        "client": request.client.host if request.client else None,
        "headers": headers_of_interest,
        "resolved_user": resolved,
        "denied": denied,
    }


@app.get("/api/me")
async def me_api(request: Request):
    user = resolve_user(request)
    cfg = config.load_config()
    if user.is_admin:
        plates = cfg.plates
    else:
        plates = [p for p in cfg.plates if p.id in user.allowed_plate_ids]
    return {
        "email": user.email,
        "is_admin": user.is_admin,
        "is_lan": user.is_lan,
        "pending": bool(user.stored and user.stored.pending),
        "plates": [
            {
                "id": p.id,
                "name": p.name,
                "plate_width": p.plate_width,
                "plate_height": p.plate_height,
                "overlay_id": p.overlay_id,
                "overlay_page": p.overlay_page,
                "has_ip": bool(p.ip_address),
                # Admins also get topic_prefix + ip_address via /api/config.
            }
            for p in plates
        ],
    }


@app.get("/api/users", response_model=list[User])
async def list_users_api(_: ResolvedUser = Depends(require_admin)):
    return users_store.list_users()


@app.put("/api/users/{email}", response_model=User)
async def upsert_user_api(
    email: str,
    body: UserUpdate,
    admin: ResolvedUser = Depends(require_admin),
):
    email = email.strip().lower()
    existing = users_store.get_user(email)
    data = body.model_dump(exclude_unset=True)
    if existing is None:
        # Mirror the add to the Cloudflare Access policy (best-effort).
        cfg = config.load_config()
        cf_result = await cloudflare.add_user(cfg.cloudflare, email)
        if not cf_result.ok:
            log.warning("CF add_user(%s) failed: %s", email, cf_result.detail)
        else:
            # Persist any newly-cached UIDs.
            config.save_config(cfg)
        new_user = User(
            email=email,
            name=data.get("name", ""),
            is_admin=bool(data.get("is_admin", False)),
            allowed_plate_ids=list(data.get("allowed_plate_ids") or []),
        )
        saved = users_store.upsert_user(new_user)
        if not cf_result.ok and cfg.cloudflare.enabled:
            # Surface the CF failure to the caller, but the local user is
            # already saved — admin can retry the sync separately.
            raise HTTPException(
                status_code=502,
                detail=f"Local user saved, but CF sync failed: {cf_result.detail}",
            )
        return saved
    # Update in place
    if "name" in data:
        existing.name = data["name"]
    if "is_admin" in data:
        # Don't allow the only remaining admin to demote themselves and lock
        # everyone out — but LAN bypass means it's never *actually* locked
        # out, so we just warn via the response detail if applicable.
        existing.is_admin = bool(data["is_admin"])
    if "allowed_plate_ids" in data:
        existing.allowed_plate_ids = list(data["allowed_plate_ids"] or [])
    # Any admin attention clears the pending flag.
    existing.pending = False
    return users_store.upsert_user(existing)


@app.delete("/api/users/{email}")
async def delete_user_api(email: str, _: ResolvedUser = Depends(require_admin)):
    if not users_store.delete_user(email):
        raise HTTPException(status_code=404, detail="User not found")
    cfg = config.load_config()
    cf_result = await cloudflare.remove_user(cfg.cloudflare, email)
    if cf_result.ok:
        config.save_config(cfg)
    elif cfg.cloudflare.enabled:
        log.warning("CF remove_user(%s) failed: %s", email, cf_result.detail)
        return {"ok": True, "cf_warning": cf_result.detail}
    return {"ok": True}


@app.post("/api/cloudflare/test")
async def cf_test_api(_: ResolvedUser = Depends(require_admin)):
    cfg = config.load_config()
    r = await cloudflare.verify(cfg.cloudflare)
    # Persist any cached UIDs the verify resolved.
    config.save_config(cfg)
    if not r.ok:
        raise HTTPException(status_code=502, detail=r.detail)
    # Loud reminder if creds verify but sync itself is off — that's the
    # single most common "why didn't my user add to CF?" issue.
    note = "" if cfg.cloudflare.enabled else " — but Enabled is OFF; tick it to actually sync user changes"
    return {"ok": True, "detail": r.detail + note, "enabled": cfg.cloudflare.enabled}


@app.post("/api/cloudflare/sync")
async def cf_sync_api(_: ResolvedUser = Depends(require_admin)):
    """Pull the policy's email rules and ensure each is a known user
    (pending if newly seen). Doesn't touch existing user records."""
    cfg = config.load_config()
    emails, err = await cloudflare.list_policy_emails(cfg.cloudflare)
    if err:
        raise HTTPException(status_code=502, detail=err)
    config.save_config(cfg)  # cache resolved UIDs
    added = []
    for email in emails:
        if users_store.get_user(email) is None:
            users_store.upsert_user(User(
                email=email,
                name=email.split("@")[0],
                is_admin=False,
                allowed_plate_ids=[],
                pending=True,
            ))
            added.append(email)
    return {"ok": True, "policy_emails": emails, "added_pending": added}


# --- WebSocket ---

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    # Resolve the user from the same Cloudflare header on the WS request.
    try:
        user = resolve_user(ws)  # type: ignore[arg-type]  (Request-compatible: .headers)
    except HTTPException as he:
        await ws.close(code=4403, reason=str(he.detail))
        return

    await state.ws.connect(ws, user=user)
    # Push display state for every plate on connect (filtered per user).
    for plate_id, ps in state.plate_states.items():
        if ps.current_spec is None:
            continue
        if not user.can_access_plate(plate_id):
            continue
        await ws.send_json({
            "type": "display_state",
            "plate_id": plate_id,
            "active": True,
            "active_until": ps.active_until,
            "spec": ps.current_spec.model_dump(),
        })
    try:
        while True:
            data = await ws.receive_json()
            kind = data.get("type")
            if kind == "ping":
                await ws.send_json({"type": "pong"})
                continue

            plate_id = data.get("plate_id")
            if kind in ("preview", "send", "clear"):
                if not plate_id:
                    await ws.send_json({"type": "error", "error": "missing plate_id"})
                    continue
                if not user.can_access_plate(plate_id):
                    await ws.send_json({"type": "error", "error": "No access to this plate"})
                    continue

            if kind == "preview":
                await state.ws.broadcast(
                    {"type": "preview", "plate_id": plate_id, "spec": data.get("spec")},
                    exclude=ws,
                )
            elif kind == "send":
                try:
                    spec = MessageSpec.model_validate(data.get("spec") or {})
                except Exception as e:  # noqa: BLE001
                    await ws.send_json({"type": "error", "error": f"Invalid spec: {e}"})
                    continue
                try:
                    plate = _get_plate(plate_id)
                    await _do_send(
                        plate,
                        SendRequest(spec=spec, duration_seconds=data.get("duration_seconds")),
                    )
                except HTTPException as he:
                    await ws.send_json({"type": "error", "error": he.detail})
            elif kind == "clear":
                try:
                    plate = _get_plate(plate_id)
                    await _do_clear(plate)
                except HTTPException as he:
                    await ws.send_json({"type": "error", "error": he.detail})
    except WebSocketDisconnect:
        pass
    finally:
        await state.ws.disconnect(ws)

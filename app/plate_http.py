"""Direct HTTP control of the plate via OpenHASP's /api/config endpoints.

Used by the "Push MQTT config to plate" admin action: writes the current
broker settings into the plate's config over HTTP (no auth on the plate's
web interface in our setup) and triggers a reboot so the new MQTT client
connects to the chosen broker.

Keeps the plate's `name` and `topic` fields intact — the plate's hostname
is what forms its topic prefix (`hasp/<name>/...`) and we don't want to
change the message routing just because we moved it between locations.
"""

from __future__ import annotations

import logging
import socket
from typing import Optional

import httpx

from .models import BrokerConfig, Plate

log = logging.getLogger("hasp.plate_http")


_LOOPBACK_NAMES = {"localhost", "0.0.0.0", "::", "::1"}


def _is_loopback(host: str) -> bool:
    """True if `host` resolves to a loopback the plate can't reach."""
    h = (host or "").strip().lower()
    if not h:
        return False
    if h in _LOOPBACK_NAMES:
        return True
    # 127.0.0.0/8
    return h.startswith("127.")


def _lan_ip_toward(plate_ip: str) -> Optional[str]:
    """Return the local IP the OS would use to reach `plate_ip`.

    UDP connect is a no-op on the wire — it just makes the kernel pick the
    source address, which is what we want: the right local interface for
    this specific destination (handles multi-homed hosts, Tailscale, VLANs
    etc. without hard-coding anything).
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((plate_ip, 1))
            return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return None


def _broker_host_for_plate(broker_host: str, plate_ip: str) -> str:
    """When the server config says the broker is on localhost, the plate
    obviously can't reach that — swap in the LAN IP of this host (as seen
    from the plate's perspective) so the plate can actually connect."""
    if not _is_loopback(broker_host):
        return broker_host
    lan_ip = _lan_ip_toward(plate_ip) if plate_ip else None
    if not lan_ip or lan_ip.startswith("127."):
        # Fallback — couldn't figure it out. Leave the host as-is; admin
        # can either fix the broker setting or the plate IP and retry.
        return broker_host
    log.info("Substituting broker host %r -> %s for plate at %s",
             broker_host, lan_ip, plate_ip)
    return lan_ip


class PlateHttpResult:
    __slots__ = ("ok", "detail")

    def __init__(self, ok: bool, detail: str = "") -> None:
        self.ok = ok
        self.detail = detail


def _base_url(plate: Plate) -> Optional[str]:
    ip = (plate.ip_address or "").strip()
    if not ip:
        return None
    if "://" in ip:
        return ip.rstrip("/")
    return f"http://{ip}"


def resolve_push_payload(plate: Plate, broker: BrokerConfig) -> dict:
    """Return what `push_mqtt_config` would POST to the plate (sans password),
    for UI preview before confirming."""
    return {
        "host": _broker_host_for_plate(broker.host, plate.ip_address or ""),
        "port": int(broker.port) if broker.port else 1883,
        "user": broker.username or "",
    }


async def push_mqtt_config(plate: Plate, broker: BrokerConfig) -> PlateHttpResult:
    """POST the broker's connection details into the plate's MQTT config.

    Only the connection fields are sent (host/port/user/pass) — topic
    templates and the plate's `name` are preserved on the plate itself so
    moving to a new broker doesn't accidentally change which topic prefix
    this plate publishes/subscribes to.
    """
    base = _base_url(plate)
    if base is None:
        return PlateHttpResult(False, "Plate has no IP address set")
    body = {
        "host": _broker_host_for_plate(broker.host, plate.ip_address or ""),
        "port": int(broker.port) if broker.port else 1883,
        "user": broker.username or "",
        "pass": broker.password or "",
    }
    url = f"{base}/api/config/mqtt/"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, json=body)
    except httpx.RequestError as e:
        return PlateHttpResult(False, f"Fetch failed ({type(e).__name__}): {e}")
    if r.status_code != 200:
        return PlateHttpResult(False, f"Plate returned HTTP {r.status_code}: {r.text[:200]}")
    log.info("Pushed MQTT config to %s (%s): host=%s port=%s",
             plate.name, plate.ip_address, body["host"], body["port"])
    return PlateHttpResult(True, f"Config pushed to {plate.name}")


async def reboot(plate: Plate) -> PlateHttpResult:
    """Trigger a plate reboot via GET /reboot.

    OpenHASP's main page links to /reboot as a plain anchor, so GET is the
    documented way. The plate drops the HTTP connection immediately as it
    restarts, which looks like an error — we treat any 2xx/0 body as success
    and only report failure on an explicit HTTP error status.
    """
    base = _base_url(plate)
    if base is None:
        return PlateHttpResult(False, "Plate has no IP address set")
    url = f"{base}/reboot"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
            if r.status_code >= 400:
                return PlateHttpResult(False, f"Reboot returned HTTP {r.status_code}")
    except httpx.ReadError:
        # The plate yanks the connection as it restarts — treat as success.
        pass
    except httpx.RequestError as e:
        return PlateHttpResult(False, f"Reboot failed ({type(e).__name__}): {e}")
    log.info("Rebooted plate %s (%s)", plate.name, plate.ip_address)
    return PlateHttpResult(True, "Rebooting")

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from fastapi import WebSocket

log = logging.getLogger("hasp.ws")


class WsManager:
    def __init__(self) -> None:
        # Map ws -> user object (set by the endpoint on connect). The user
        # exposes can_access_plate(plate_id) so we can filter broadcasts.
        self._clients: dict[WebSocket, Any] = {}
        self._lock = asyncio.Lock()
        self._last_state: dict[str, Any] = {}

    async def connect(self, ws: WebSocket, user: Any = None) -> None:
        await ws.accept()
        async with self._lock:
            self._clients[ws] = user
        # Replay cached state messages, filtered per the client's permissions.
        for snap in self._last_state.values():
            if not self._user_can_see(user, snap):
                continue
            try:
                await ws.send_text(json.dumps(snap))
            except Exception:  # noqa: BLE001
                pass

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.pop(ws, None)

    @staticmethod
    def _user_can_see(user: Any, message: dict) -> bool:
        plate_id = message.get("plate_id")
        if not plate_id:
            # Global message (mqtt_state, errors) — everyone sees it.
            return True
        if user is None:
            # LAN/no-user contexts (shouldn't happen post-auth) — allow.
            return True
        check = getattr(user, "can_access_plate", None)
        if check is None:
            return True
        return bool(check(plate_id))

    async def broadcast(self, message: dict, exclude: WebSocket | None = None) -> None:
        kind = message.get("type")
        # Don't cache ephemeral previews — they are per-edit, not state
        if kind and kind != "preview":
            self._last_state[kind] = message
        body = json.dumps(message)
        dead: list[WebSocket] = []
        async with self._lock:
            clients = list(self._clients.items())
        for ws, user in clients:
            if ws is exclude:
                continue
            if not self._user_can_see(user, message):
                continue
            try:
                await ws.send_text(body)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.pop(ws, None)

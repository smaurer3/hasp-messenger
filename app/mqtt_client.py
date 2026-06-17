from __future__ import annotations

import asyncio
import json
import logging
import ssl
import threading
from typing import Awaitable, Callable, Optional

import paho.mqtt.client as mqtt

from .models import BrokerConfig, Plate

log = logging.getLogger("hasp.mqtt")


CONTROL_TOPIC_PREFIX = "hasp-messenger"
CONTROL_SUB_TOPIC = f"{CONTROL_TOPIC_PREFIX}/+/+"


class MqttClient:
    def __init__(
        self,
        cfg: BrokerConfig,
        on_state: Callable[[dict], Awaitable[None]],
        loop: asyncio.AbstractEventLoop,
        on_template_trigger: Optional[Callable[[str, str, dict], Awaitable[None]]] = None,
    ):
        self._cfg = cfg
        self._on_state = on_state
        self._on_template_trigger = on_template_trigger
        self._loop = loop
        self._client: Optional[mqtt.Client] = None
        self._connected = False
        self._last_error: Optional[str] = None
        self._lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def _command_topic(self, plate: Plate) -> str:
        return f"{plate.topic_prefix}/command/jsonl"

    def _lwt_topic(self, plate: Plate) -> str:
        return f"{plate.topic_prefix}/LWT"

    def update_config(self, cfg: BrokerConfig) -> None:
        with self._lock:
            self._cfg = cfg
        self.reconnect()

    def start(self) -> None:
        self.reconnect()

    def stop(self) -> None:
        c = self._client
        if c is not None:
            try:
                c.loop_stop()
                c.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._client = None
        self._connected = False
        self._emit_state()

    def reconnect(self) -> None:
        self.stop()
        cfg = self._cfg
        if not cfg.host:
            self._last_error = "Broker host not configured"
            self._emit_state()
            return

        c = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=cfg.client_id or "",
            clean_session=True,
        )
        if cfg.username:
            c.username_pw_set(cfg.username, cfg.password or None)
        if cfg.use_tls:
            ctx = ssl.create_default_context()
            c.tls_set_context(ctx)

        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        try:
            c.connect_async(cfg.host, cfg.port, keepalive=30)
        except Exception as e:  # noqa: BLE001
            self._last_error = f"connect_async: {e}"
            log.exception("mqtt connect_async failed")
            self._emit_state()
            return

        c.loop_start()
        self._client = c

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0 or getattr(reason_code, "is_failure", True) is False:
            self._connected = True
            self._last_error = None
            for plate in self._cfg.plates:
                try:
                    client.subscribe(self._lwt_topic(plate))
                except Exception:  # noqa: BLE001
                    pass
            # Subscribe once to the template-trigger control topic. Anyone
            # publishing to hasp-messenger/<plate-slug>/<template-slug> causes
            # the matching saved template to be sent to the matching plate.
            try:
                client.subscribe(CONTROL_SUB_TOPIC)
            except Exception:  # noqa: BLE001
                pass
            log.info("MQTT connected to %s:%s (%d plates)",
                     self._cfg.host, self._cfg.port, len(self._cfg.plates))
        else:
            self._connected = False
            self._last_error = f"connect rc={reason_code}"
        self._emit_state()

    def _on_disconnect(self, client, userdata, *args, **kwargs):
        self._connected = False
        log.info("MQTT disconnected")
        self._emit_state()

    def _on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return
        # Template trigger: hasp-messenger/<plate-slug>/<template-slug>
        parts = msg.topic.split("/")
        if (
            len(parts) == 3
            and parts[0] == CONTROL_TOPIC_PREFIX
            and self._on_template_trigger is not None
        ):
            plate_slug, template_slug = parts[1], parts[2]
            try:
                body = json.loads(payload) if payload.strip() else {}
            except Exception:  # noqa: BLE001
                body = {}
            if not isinstance(body, dict):
                body = {}
            asyncio.run_coroutine_threadsafe(
                self._on_template_trigger(plate_slug, template_slug, body),
                self._loop,
            )
            return
        for plate in self._cfg.plates:
            if msg.topic == self._lwt_topic(plate):
                asyncio.run_coroutine_threadsafe(
                    self._on_state({
                        "type": "plate_lwt",
                        "plate_id": plate.id,
                        "value": payload,
                    }),
                    self._loop,
                )
                return

    def publish_jsonl(self, plate: Plate, payload: dict) -> tuple[bool, Optional[str]]:
        c = self._client
        if c is None or not self._connected:
            return False, "Not connected"
        try:
            body = json.dumps(payload)
            info = c.publish(self._command_topic(plate), body, qos=0, retain=False)
            info.wait_for_publish(timeout=5.0)
            return info.rc == mqtt.MQTT_ERR_SUCCESS, (
                None if info.rc == mqtt.MQTT_ERR_SUCCESS else f"publish rc={info.rc}"
            )
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    def publish_clear(self, plate: Plate) -> tuple[bool, Optional[str]]:
        # UPDATE (no "obj"): just hides the existing overlay in place.
        payload = {
            "page": plate.overlay_page,
            "id": plate.overlay_id,
            "hidden": True,
        }
        return self.publish_jsonl(plate, payload)

    def publish_init(self, plate: Plate) -> tuple[bool, Optional[str]]:
        # CREATE-or-replace the overlay label hidden, so subsequent sends
        # can be lightweight updates.
        payload = {
            "page": plate.overlay_page,
            "id": plate.overlay_id,
            "obj": "label",
            "x": 0, "y": 0,
            "w": plate.plate_width,
            "h": plate.plate_height,
            "text": "",
            "hidden": True,
            "bg_opa": 0,
        }
        return self.publish_jsonl(plate, payload)

    def _emit_state(self) -> None:
        state = {
            "type": "mqtt_state",
            "connected": self._connected,
            "host": self._cfg.host,
            "port": self._cfg.port,
            "tls": self._cfg.use_tls,
            "plate_count": len(self._cfg.plates),
            "last_error": self._last_error,
        }
        try:
            asyncio.run_coroutine_threadsafe(self._on_state(state), self._loop)
        except RuntimeError:
            pass

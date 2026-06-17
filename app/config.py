from __future__ import annotations

import json
import threading
from pathlib import Path

from .models import BrokerConfig, Plate

CONFIG_PATH = Path(__file__).resolve().parent.parent / "data" / "config.json"
_lock = threading.Lock()


def _migrate(raw: dict) -> dict:
    """Migrate older single-plate config layout to the plates list layout."""
    if "plates" not in raw:
        plate = {
            "id": "default",
            "name": "Plate 1",
            "topic_prefix": raw.pop("topic_prefix", "hasp/plate"),
            "overlay_id": raw.pop("overlay_id", 240),
            "overlay_page": raw.pop("overlay_page", 1),
            "plate_width": raw.pop("plate_width", 480),
            "plate_height": raw.pop("plate_height", 320),
        }
        raw["plates"] = [plate]
    return raw


def load_config() -> BrokerConfig:
    if not CONFIG_PATH.exists():
        cfg = BrokerConfig(plates=[Plate(id="default", name="Plate 1")])
        save_config(cfg)
        return cfg
    with _lock:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    raw = _migrate(raw)
    cfg = BrokerConfig.model_validate(raw)
    if not cfg.plates:
        cfg.plates = [Plate(id="default", name="Plate 1")]
    return cfg


def save_config(cfg: BrokerConfig) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        CONFIG_PATH.write_text(
            json.dumps(cfg.model_dump(), indent=2), encoding="utf-8"
        )

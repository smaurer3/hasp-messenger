from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from .models import Template

TEMPLATES_PATH = Path(__file__).resolve().parent.parent / "data" / "templates.json"
_lock = threading.Lock()


def _read_all() -> list[dict]:
    if not TEMPLATES_PATH.exists():
        return []
    with _lock:
        return json.loads(TEMPLATES_PATH.read_text(encoding="utf-8"))


def _write_all(items: list[dict]) -> None:
    TEMPLATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        TEMPLATES_PATH.write_text(json.dumps(items, indent=2), encoding="utf-8")


def list_templates(plate_id: Optional[str] = None) -> list[Template]:
    items = _read_all()
    if plate_id is not None:
        items = [x for x in items if x.get("plate_id") == plate_id]
    return [Template.model_validate(x) for x in items]


def migrate_orphan_templates(default_plate_id: Optional[str]) -> int:
    """Assign any pre-existing template without a plate_id to the given plate.

    Run once at startup. Returns the number migrated. Without this, templates
    saved before the per-plate scoping was added would become invisible.
    """
    if not default_plate_id:
        return 0
    items = _read_all()
    n = 0
    for x in items:
        if not x.get("plate_id"):
            x["plate_id"] = default_plate_id
            n += 1
    if n:
        _write_all(items)
    return n


def get_template(tid: str) -> Optional[Template]:
    for x in _read_all():
        if x.get("id") == tid:
            return Template.model_validate(x)
    return None


def create_template(tpl: Template) -> Template:
    items = _read_all()
    now = time.time()
    tpl.id = uuid.uuid4().hex
    tpl.created_at = now
    tpl.updated_at = now
    items.append(tpl.model_dump())
    _write_all(items)
    return tpl


def update_template(tid: str, tpl: Template) -> Optional[Template]:
    items = _read_all()
    for i, x in enumerate(items):
        if x.get("id") == tid:
            tpl.id = tid
            tpl.created_at = x.get("created_at")
            tpl.updated_at = time.time()
            items[i] = tpl.model_dump()
            _write_all(items)
            return tpl
    return None


def delete_template(tid: str) -> bool:
    items = _read_all()
    new_items = [x for x in items if x.get("id") != tid]
    if len(new_items) == len(items):
        return False
    _write_all(new_items)
    return True

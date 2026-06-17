from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Optional

from .models import User

USERS_PATH = Path(__file__).resolve().parent.parent / "data" / "users.json"
_lock = threading.Lock()


def _read_all() -> list[dict]:
    if not USERS_PATH.exists():
        return []
    with _lock:
        return json.loads(USERS_PATH.read_text(encoding="utf-8"))


def _write_all(items: list[dict]) -> None:
    USERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        USERS_PATH.write_text(json.dumps(items, indent=2), encoding="utf-8")


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def list_users() -> list[User]:
    return [User.model_validate(x) for x in _read_all()]


def get_user(email: str) -> Optional[User]:
    e = _norm(email)
    for x in _read_all():
        if _norm(x.get("email")) == e:
            return User.model_validate(x)
    return None


def upsert_user(user: User) -> User:
    user.email = _norm(user.email)
    items = _read_all()
    now = time.time()
    for i, x in enumerate(items):
        if _norm(x.get("email")) == user.email:
            user.created_at = x.get("created_at") or now
            user.updated_at = now
            items[i] = user.model_dump()
            _write_all(items)
            return user
    user.created_at = now
    user.updated_at = now
    items.append(user.model_dump())
    _write_all(items)
    return user


def delete_user(email: str) -> bool:
    e = _norm(email)
    items = _read_all()
    new_items = [x for x in items if _norm(x.get("email")) != e]
    if len(new_items) == len(items):
        return False
    _write_all(new_items)
    return True


def count_users() -> int:
    return len(_read_all())

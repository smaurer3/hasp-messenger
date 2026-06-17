from __future__ import annotations

import uuid
from typing import Optional, Literal
from pydantic import BaseModel, Field


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


class Plate(BaseModel):
    id: str = Field(default_factory=_new_id)
    name: str = "Plate"
    topic_prefix: str = "hasp/plate"
    overlay_id: int = 240
    overlay_page: int = 1
    plate_width: int = 480
    plate_height: int = 320
    # HTTP address of the plate (hostname or IP). Used by the server to fetch
    # snapshots via OpenHASP's /screenshot endpoint and proxy them to clients.
    ip_address: str = ""


class CloudflareConfig(BaseModel):
    enabled: bool = False
    api_token: str = ""
    account_id: str = ""
    application_name: str = ""
    policy_name: str = ""
    # Cached after the first successful name → UUID resolution. Cleared if
    # the upstream lookup fails so we re-resolve next time.
    application_uid: str = ""
    policy_uid: str = ""


class BrokerConfig(BaseModel):
    host: str = ""
    port: int = 8883
    username: str = ""
    password: str = ""
    use_tls: bool = True
    client_id: str = "hasp-messenger"
    plates: list[Plate] = Field(default_factory=list)
    cloudflare: CloudflareConfig = Field(default_factory=CloudflareConfig)


class CloudflareConfigPublic(BaseModel):
    enabled: bool
    account_id: str
    application_name: str
    policy_name: str
    application_uid: str
    policy_uid: str
    api_token_set: bool


class BrokerConfigPublic(BaseModel):
    host: str
    port: int
    username: str
    password_set: bool
    use_tls: bool
    client_id: str
    plates: list[Plate]
    cloudflare: CloudflareConfigPublic


class MessageSpec(BaseModel):
    text: str = ""
    x: int = 10
    y: int = 85
    w: int = 460
    h: int = 150
    text_font: int = 48
    text_color: str = "#FFFFFF"
    bg_color: str = "#FF0000"
    bg_opa: int = Field(default=255, ge=0, le=255)
    align: Literal["left", "center", "right"] = "center"
    mode: Literal["expand", "break", "dots", "scroll", "loop", "crop"] = "break"
    pad_top: int = 25
    pad_bottom: int = 25
    pad_left: int = 0
    pad_right: int = 0

    def to_hasp_payload(self, overlay_id: int, page: int) -> dict:
        return {
            "page": page,
            "id": overlay_id,
            "obj": "label",
            "x": self.x,
            "y": self.y,
            "w": self.w,
            "h": self.h,
            "text": self.text,
            "text_font": self.text_font,
            "text_color": self.text_color,
            "align": self.align,
            "mode": self.mode,
            "bg_opa": self.bg_opa,
            "bg_color": self.bg_color,
            "hidden": False,
            "pad_top": self.pad_top,
            "pad_bottom": self.pad_bottom,
            "pad_left": self.pad_left,
            "pad_right": self.pad_right,
        }


class Template(BaseModel):
    id: Optional[str] = None
    name: str
    spec: MessageSpec
    # Each template belongs to one plate. Filtered in the UI by current plate.
    plate_id: Optional[str] = None
    # Auto-clear timer baked into the template. Used when an MQTT trigger
    # doesn't supply its own `duration_seconds` override.
    duration_seconds: Optional[float] = None
    created_at: Optional[float] = None
    updated_at: Optional[float] = None


class SendRequest(BaseModel):
    spec: MessageSpec
    duration_seconds: Optional[float] = None


class User(BaseModel):
    """A user authenticated through Cloudflare Access (Google SSO).

    `is_admin=True` users implicitly have access to every plate; the
    `allowed_plate_ids` list is only consulted for non-admins.

    `pending=True` marks a user who landed on the app via CF but hasn't been
    reviewed by an admin yet. They see a "waiting for approval" screen; admins
    see them flagged in the Users page. The flag clears the first time an
    admin updates the record.
    """
    email: str
    name: str = ""
    is_admin: bool = False
    allowed_plate_ids: list[str] = Field(default_factory=list)
    pending: bool = False
    created_at: Optional[float] = None
    updated_at: Optional[float] = None


class UserUpdate(BaseModel):
    name: Optional[str] = None
    is_admin: Optional[bool] = None
    allowed_plate_ids: Optional[list[str]] = None

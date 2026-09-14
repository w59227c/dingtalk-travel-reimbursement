from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.security import random_token, token_hash, tokens_match

_TICKET_VERSION = 1
_MAX_TICKET_LENGTH = 2048
_MAX_TICKET_LIFETIME = timedelta(minutes=5)


class InvalidFilePreviewTicket(ValueError):
    """The native original-file download credential is invalid or expired."""


@dataclass(frozen=True, slots=True)
class FilePreviewTicket:
    draft_id: str
    file_id: str
    session_id_hash: str
    expires_at: datetime


def issue_file_preview_ticket(
    *,
    draft_id: str,
    file_id: str,
    session_id_hash: str,
    secret: str,
    lifetime: timedelta = timedelta(seconds=60),
    now: datetime | None = None,
) -> str:
    issued_at = _as_utc(now or datetime.now(UTC))
    if not draft_id or len(draft_id) > 128:
        raise ValueError("draft_id is invalid")
    if not file_id or len(file_id) > 128:
        raise ValueError("file_id is invalid")
    if len(session_id_hash) != 64:
        raise ValueError("session_id_hash is invalid")
    if lifetime <= timedelta(0) or lifetime > _MAX_TICKET_LIFETIME:
        raise ValueError("ticket lifetime is invalid")
    payload = {
        "v": _TICKET_VERSION,
        "d": draft_id,
        "f": file_id,
        "s": session_id_hash,
        "e": int((issued_at + lifetime).timestamp()),
        "n": random_token(),
    }
    encoded = _encode_payload(payload)
    return f"{encoded}.{token_hash(encoded, secret)}"


def verify_file_preview_ticket(
    token: str,
    *,
    secret: str,
    now: datetime | None = None,
) -> FilePreviewTicket:
    checked_at = _as_utc(now or datetime.now(UTC))
    if not token or len(token) > _MAX_TICKET_LENGTH:
        raise InvalidFilePreviewTicket("ticket is missing or too long")
    try:
        encoded, signature = token.split(".")
    except ValueError:
        raise InvalidFilePreviewTicket("ticket shape is invalid") from None
    if len(signature) != 64 or not tokens_match(encoded, signature, secret):
        raise InvalidFilePreviewTicket("ticket signature is invalid")
    try:
        payload = json.loads(_decode_payload(encoded))
    except (UnicodeDecodeError, ValueError):
        raise InvalidFilePreviewTicket("ticket payload is invalid") from None
    if not isinstance(payload, dict) or set(payload) != {"v", "d", "f", "s", "e", "n"}:
        raise InvalidFilePreviewTicket("ticket claims are invalid")
    version = payload["v"]
    draft_id = payload["d"]
    file_id = payload["f"]
    session_id_hash = payload["s"]
    expires_at_seconds = payload["e"]
    nonce = payload["n"]
    if version != _TICKET_VERSION:
        raise InvalidFilePreviewTicket("ticket version is invalid")
    if not isinstance(draft_id, str) or not draft_id or len(draft_id) > 128:
        raise InvalidFilePreviewTicket("ticket draft is invalid")
    if not isinstance(file_id, str) or not file_id or len(file_id) > 128:
        raise InvalidFilePreviewTicket("ticket file is invalid")
    if not isinstance(session_id_hash, str) or len(session_id_hash) != 64:
        raise InvalidFilePreviewTicket("ticket session is invalid")
    if (
        isinstance(expires_at_seconds, bool)
        or not isinstance(expires_at_seconds, int)
        or not isinstance(nonce, str)
        or len(nonce) < 32
    ):
        raise InvalidFilePreviewTicket("ticket expiry is invalid")
    expires_at = datetime.fromtimestamp(expires_at_seconds, UTC)
    if expires_at <= checked_at or expires_at - checked_at > _MAX_TICKET_LIFETIME:
        raise InvalidFilePreviewTicket("ticket has expired")
    return FilePreviewTicket(
        draft_id=draft_id,
        file_id=file_id,
        session_id_hash=session_id_hash,
        expires_at=expires_at,
    )


def _encode_payload(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_payload(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(
        f"{value}{padding}",
        altchars=b"-_",
        validate=True,
    ).decode("utf-8")


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

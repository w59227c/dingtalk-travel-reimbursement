from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.security import random_token, token_hash, tokens_match

_TICKET_VERSION = 1
_MAX_TICKET_LENGTH = 2048
_MAX_TICKET_LIFETIME = timedelta(minutes=5)


class InvalidExcelPreviewTicket(ValueError):
    """The native download credential is malformed, expired, or untrusted."""


@dataclass(frozen=True, slots=True)
class ExcelPreviewTicket:
    draft_id: str
    expected_revision: int
    session_id_hash: str
    expires_at: datetime


def issue_excel_preview_ticket(
    *,
    draft_id: str,
    expected_revision: int,
    session_id_hash: str,
    secret: str,
    lifetime: timedelta = timedelta(seconds=60),
    now: datetime | None = None,
) -> str:
    issued_at = _as_utc(now or datetime.now(UTC))
    if not draft_id or len(draft_id) > 128:
        raise ValueError("draft_id is invalid")
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 1
    ):
        raise ValueError("expected_revision is invalid")
    if len(session_id_hash) != 64:
        raise ValueError("session_id_hash is invalid")
    if lifetime <= timedelta(0) or lifetime > _MAX_TICKET_LIFETIME:
        raise ValueError("ticket lifetime is invalid")
    payload = {
        "v": _TICKET_VERSION,
        "d": draft_id,
        "r": expected_revision,
        "s": session_id_hash,
        "e": int((issued_at + lifetime).timestamp()),
        "n": random_token(),
    }
    encoded = _encode_payload(payload)
    return f"{encoded}.{token_hash(encoded, secret)}"


def verify_excel_preview_ticket(
    token: str,
    *,
    secret: str,
    now: datetime | None = None,
) -> ExcelPreviewTicket:
    checked_at = _as_utc(now or datetime.now(UTC))
    if not token or len(token) > _MAX_TICKET_LENGTH:
        raise InvalidExcelPreviewTicket("ticket is missing or too long")
    try:
        encoded, signature = token.split(".")
    except ValueError:
        raise InvalidExcelPreviewTicket("ticket shape is invalid") from None
    if len(signature) != 64 or not tokens_match(encoded, signature, secret):
        raise InvalidExcelPreviewTicket("ticket signature is invalid")
    try:
        payload = json.loads(_decode_payload(encoded))
    except (UnicodeDecodeError, ValueError):
        raise InvalidExcelPreviewTicket("ticket payload is invalid") from None
    if not isinstance(payload, dict) or set(payload) != {"v", "d", "r", "s", "e", "n"}:
        raise InvalidExcelPreviewTicket("ticket claims are invalid")
    version = payload["v"]
    draft_id = payload["d"]
    revision = payload["r"]
    session_id_hash = payload["s"]
    expires_at_seconds = payload["e"]
    nonce = payload["n"]
    if version != _TICKET_VERSION:
        raise InvalidExcelPreviewTicket("ticket version is invalid")
    if not isinstance(draft_id, str) or not draft_id or len(draft_id) > 128:
        raise InvalidExcelPreviewTicket("ticket draft is invalid")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise InvalidExcelPreviewTicket("ticket revision is invalid")
    if not isinstance(session_id_hash, str) or len(session_id_hash) != 64:
        raise InvalidExcelPreviewTicket("ticket session is invalid")
    if (
        isinstance(expires_at_seconds, bool)
        or not isinstance(expires_at_seconds, int)
        or not isinstance(nonce, str)
        or len(nonce) < 32
    ):
        raise InvalidExcelPreviewTicket("ticket expiry is invalid")
    expires_at = datetime.fromtimestamp(expires_at_seconds, UTC)
    if expires_at <= checked_at or expires_at - checked_at > _MAX_TICKET_LIFETIME:
        raise InvalidExcelPreviewTicket("ticket has expired")
    return ExcelPreviewTicket(
        draft_id=draft_id,
        expected_revision=revision,
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

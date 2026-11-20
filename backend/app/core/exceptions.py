"""Transport-independent errors shared by the API and isolated file workers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ApiError(Exception):
    code: str
    message: str
    status_code: int = 400

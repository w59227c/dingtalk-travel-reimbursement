from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

from app.domain.categories import ExpenseCategory


@dataclass(frozen=True, slots=True)
class OcrLine:
    text: str
    confidence: float


@dataclass(frozen=True, slots=True)
class ParseContext:
    reference_year: int
    invoice_qr: InvoiceQrEvidence | None = None


@dataclass(frozen=True, slots=True)
class InvoiceQrEvidence:
    """Validated fields decoded from a legacy invoice QR payload.

    The QR amount is deliberately only evidence. Depending on invoice type it
    may be tax-exclusive, so parsers must reconcile it with visible totals and
    tax instead of treating it as the reimbursement amount unconditionally.
    """

    amount: Decimal | None = None
    issue_date: date | None = None


@dataclass(frozen=True, slots=True)
class ReceiptKeywordRule:
    keyword: str
    category: ExpenseCategory
    transport_type: Literal["ride_hailing", "taxi"] | None = None


@dataclass(frozen=True, slots=True)
class ParsedExpense:
    receipt_type: str
    category: ExpenseCategory
    date: date | None
    description: str | None
    amount: Decimal | None
    confidence: float
    warnings: tuple[str, ...] = field(default_factory=tuple)
    requires_itinerary: bool = False
    transport_type: Literal["ride_hailing", "taxi", "rail", "hotel", "other"] | None = None
    original_currency: str | None = None
    original_amount: Decimal | None = None
    invoice_numbers: tuple[str, ...] = field(default_factory=tuple)
    order_numbers: tuple[str, ...] = field(default_factory=tuple)
    rail_type: Literal["high_speed", "emu", "regular", "unknown"] | None = None


@dataclass(frozen=True, slots=True)
class ItinerarySummary:
    currency: str | None = None
    amount: Decimal | None = None
    start_date: date | None = None
    end_date: date | None = None
    invoice_numbers: tuple[str, ...] = ()
    order_numbers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ItineraryTrip:
    page: int
    row: int
    date: date | None = None
    amount: Decimal | None = None
    origin: str | None = None
    destination: str | None = None
    invoice_numbers: tuple[str, ...] = ()
    order_numbers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ParsedItinerary:
    source: Literal["pdf_text", "paddle", "mixed", "unknown"]
    page_count: int
    processed_page_count: int
    complete: bool
    summary: ItinerarySummary = field(default_factory=ItinerarySummary)
    trips: tuple[ItineraryTrip, ...] = ()
    warnings: tuple[str, ...] = ()


class LocalOcrEngine(Protocol):
    is_fake: bool

    def ensure_ready(self) -> None: ...

    def recognize(self, path: str) -> list[OcrLine]: ...


class OcrEngineConfig(Protocol):
    """Minimal configuration interface required by the Paddle adapter."""

    ocr_detection_model_dir: Path | None
    ocr_recognition_model_dir: Path | None
    ocr_engine: str
    ocr_cpu_threads: int


class BaseReceiptParser(Protocol):
    def match(self, lines: list[OcrLine]) -> float: ...

    def parse(self, lines: list[OcrLine], context: ParseContext) -> ParsedExpense: ...

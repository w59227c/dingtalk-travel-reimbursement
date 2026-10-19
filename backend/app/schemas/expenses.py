from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from app.core.errors import ApiError
from app.domain.categories import ExpenseCategory
from app.domain.money import MAX_REIMBURSEMENT_AMOUNT, quantize_money
from app.domain.subsidy import TripPeriod, TripType
from app.schemas.primitives import DecimalString, MinuteTime, StrictCalendarDate

MAX_CONFIRMED_EFFECTIVE_DAYS = Decimal("366.0")
MAX_EXPENSE_ITEMS_HARD_LIMIT = 1_000


class TripPurpose(StrEnum):
    BUSINESS = "business"
    PROJECT = "project"
    SAME_CITY_PROJECT = "same_city_project"
    INTERNAL = "internal"
    OVERSEAS = "overseas"
    SHORT_TERM_PROJECT = "short_term_project"
    LONG_TERM_PROJECT = "long_term_project"
    SHORT_TERM_INTERNAL = "short_term_internal"
    LONG_TERM_INTERNAL = "long_term_internal"
    SHORT_TERM_OVERSEAS = "short_term_overseas"
    LONG_TERM_OVERSEAS = "long_term_overseas"


class ExcludedPeriodInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    start_date: StrictCalendarDate = Field(alias="startDate")
    start_time: MinuteTime = Field(alias="startTime")
    end_date: StrictCalendarDate = Field(alias="endDate")
    end_time: MinuteTime = Field(alias="endTime")

    @field_serializer("start_time", "end_time", when_used="json")
    def serialize_minute_time(self, value: object) -> str:
        return value.strftime("%H:%M")

    def as_period(self) -> TripPeriod:
        return TripPeriod(self.start_date, self.start_time, self.end_date, self.end_time)


class TripInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    trip_type: TripPurpose = Field(alias="tripType")
    related_approval_id: str | None = Field(
        default=None, alias="relatedApprovalId", min_length=1, max_length=128
    )
    start_date: StrictCalendarDate = Field(alias="startDate")
    start_time: MinuteTime = Field(alias="startTime")
    end_date: StrictCalendarDate = Field(alias="endDate")
    end_time: MinuteTime = Field(alias="endTime")
    policy_confirmed: bool = Field(default=False, alias="policyConfirmed")
    excluded_periods: list[ExcludedPeriodInput] = Field(
        default_factory=list,
        alias="excludedPeriods",
        max_length=100,
        exclude_if=lambda value: not value,
    )
    confirmed_effective_days: DecimalString | None = Field(
        default=None,
        alias="confirmedEffectiveDays",
        ge=0,
        le=MAX_CONFIRMED_EFFECTIVE_DAYS,
    )
    no_subsidy_exception: bool = Field(default=False, alias="noSubsidyException")
    manual_subsidy_amount: DecimalString | None = Field(
        default=None,
        alias="manualSubsidyAmount",
        ge=0,
        le=MAX_REIMBURSEMENT_AMOUNT,
        exclude_if=lambda value: value is None,
    )

    @field_validator("manual_subsidy_amount")
    @classmethod
    def normalize_manual_subsidy(cls, value: Decimal | None) -> Decimal | None:
        if value is not None:
            if value.as_tuple().exponent < -2:
                raise ValueError("amount supports at most two decimal places")
            return quantize_money(value)
        return value

    @model_validator(mode="after")
    def validate_manual_subsidy_type(self) -> TripInput:
        if self.manual_subsidy_amount is not None:
            raise ValueError("manual subsidy total is no longer supported")
        return self

    def as_period(self) -> TripPeriod:
        return TripPeriod(
            start_date=self.start_date,
            start_time=self.start_time,
            end_date=self.end_date,
            end_time=self.end_time,
        )

    def subsidy_trip_type(self, *, legacy: bool = False) -> TripType:
        if self.trip_type is TripPurpose.PROJECT and legacy:
            calendar_days = (self.end_date - self.start_date).days + 1
            return TripType.LONG_TERM_PROJECT if calendar_days > 30 else TripType.SHORT_TERM_PROJECT
        if self.trip_type in {TripPurpose.PROJECT, TripPurpose.INTERNAL, TripPurpose.OVERSEAS}:
            if not legacy:
                raise ApiError(
                    "SUBSIDY_CATEGORY_CONFIRMATION_REQUIRED",
                    "请重新确认出差审批中的完整长短期类别",
                    422,
                )
        return TripType(self.trip_type.value)


class ExpenseLineBase(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    category: ExpenseCategory
    date: StrictCalendarDate | None = None
    display_date: str = Field(alias="displayDate", min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)
    amount: DecimalString = Field(ge=0, le=MAX_REIMBURSEMENT_AMOUNT)
    receipt_count: int = Field(alias="receiptCount", ge=0, le=10_000)

    @field_validator("display_date", "description")
    @classmethod
    def strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("text must not be blank")
        return stripped

    @field_validator("amount")
    @classmethod
    def normalize_amount(cls, value: Decimal) -> Decimal:
        if value.as_tuple().exponent < -2:
            raise ValueError("amount supports at most two decimal places")
        return quantize_money(value)


class ExpenseItem(ExpenseLineBase):
    id: str = Field(min_length=1, max_length=128)
    source: Literal["ocr", "manual", "system"]
    confidence: DecimalString | None = Field(default=None, ge=0, le=1)
    warnings: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("warnings")
    @classmethod
    def validate_warnings(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 200 for value in values):
            raise ValueError("warnings must be non-blank and at most 200 characters")
        return [value.strip() for value in values]

    @model_validator(mode="after")
    def validate_system_line(self) -> ExpenseItem:
        if self.category is ExpenseCategory.SUBSIDY:
            if self.source != "system" or self.receipt_count != 0:
                raise ValueError("subsidy must be a system line with zero receipts")
        elif self.source == "system":
            raise ValueError("only subsidy may be a system line")
        elif self.receipt_count == 0:
            raise ValueError("non-subsidy lines require at least one receipt")
        return self


class TotalsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # A missing trip means this reimbursement does not claim travel subsidy.
    trip: TripInput | None = None
    trips: list[TripInput] = Field(default_factory=list, max_length=20)
    items: list[ExpenseItem] = Field(max_length=MAX_EXPENSE_ITEMS_HARD_LIMIT)

    @model_validator(mode="after")
    def reject_mixed_trip_shapes(self) -> TotalsRequest:
        if self.trip is not None and self.trips:
            raise ValueError("use either trip or trips")
        ids = [item.related_approval_id for item in self.trips]
        if any(value is None for value in ids) or len(ids) != len(set(ids)):
            raise ValueError("trips require unique relatedApprovalId values")
        return self

    @field_validator("items")
    @classmethod
    def reject_client_subsidy(cls, values: list[ExpenseItem]) -> list[ExpenseItem]:
        if any(item.category is ExpenseCategory.SUBSIDY for item in values):
            raise ValueError("subsidy is calculated by the server")
        return values

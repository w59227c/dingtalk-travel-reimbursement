from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.categories import ExpenseCategory
from app.domain.money import MAX_REIMBURSEMENT_AMOUNT, quantize_money
from app.schemas.excel import ExcelGenerateRequest, ManualProjectInput, SelectedProjectInput
from app.schemas.expenses import MAX_EXPENSE_ITEMS_HARD_LIMIT, TripPurpose
from app.schemas.primitives import DecimalString, StrictCalendarDate

MAX_RELATED_APPROVALS = 20
CURRENT_OCR_DISPOSITION_VERSION = 1
RailType = Literal["high_speed", "emu", "regular", "unknown"]


class BudgetProjectInput(ManualProjectInput):
    """The complete OA option label is also the workbook project text."""

    text: str = Field(min_length=1, max_length=2048)


class ReimbursementEditingTripInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    trip_type: TripPurpose = Field(alias="tripType")
    related_approval_id: str | None = Field(
        default=None, alias="relatedApprovalId", min_length=1, max_length=128
    )
    start_date: str = Field(default="", alias="startDate", max_length=10)
    start_time: str = Field(default="", alias="startTime", max_length=5)
    end_date: str = Field(default="", alias="endDate", max_length=10)
    end_time: str = Field(default="", alias="endTime", max_length=5)
    policy_confirmed: bool = Field(default=False, alias="policyConfirmed")
    confirmed_effective_days: str = Field(default="", alias="confirmedEffectiveDays", max_length=32)
    no_subsidy_exception: bool = Field(default=False, alias="noSubsidyException")
    manual_subsidy_amount: str | None = Field(
        default=None,
        alias="manualSubsidyAmount",
        max_length=32,
        exclude_if=lambda value: value is None,
    )


class ReimbursementEditingStateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    include_subsidy: bool = Field(alias="includeSubsidy")
    trip: ReimbursementEditingTripInput
    trips: list[ReimbursementEditingTripInput] = Field(default_factory=list, max_length=20)


class ReimbursementDraftExpenseItemInput(BaseModel):
    """Persist unfinished edits; review/export separately require complete lines."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    category: ExpenseCategory
    date: StrictCalendarDate | None = None
    display_date: str = Field(default="", alias="displayDate", max_length=100)
    description: str = Field(default="", max_length=500)
    amount: DecimalString | None = Field(default=None, ge=0, le=MAX_REIMBURSEMENT_AMOUNT)
    receipt_count: int = Field(default=1, alias="receiptCount", ge=1, le=10_000)
    itinerary_file_ids: list[str] = Field(
        default_factory=list, alias="itineraryFileIds", max_length=100
    )
    itinerary_auto_match_disabled: bool = Field(
        default=False, alias="itineraryAutoMatchDisabled", strict=True
    )
    payment_proof_file_ids: list[str] = Field(
        default_factory=list, alias="paymentProofFileIds", max_length=100
    )
    hotel_bill_file_ids: list[str] = Field(
        default_factory=list, alias="hotelBillFileIds", max_length=100
    )
    rail_type: RailType = Field(default="unknown", alias="railType")
    requires_itinerary: bool = Field(default=False, alias="requiresItinerary", strict=True)
    transport_type: Literal["ride_hailing", "taxi", "rail", "hotel", "other"] | None = Field(
        default=None, alias="transportType"
    )
    original_currency: str | None = Field(
        default=None, alias="originalCurrency", pattern=r"^[A-Z]{3}$"
    )
    original_amount: DecimalString | None = Field(
        default=None, alias="originalAmount", ge=0, le=Decimal("999999999999999.99")
    )
    original_details_edited: bool = Field(
        default=False,
        alias="originalDetailsEdited",
        strict=True,
        exclude_if=lambda value: value is False,
    )
    cny_amount_confirmed: bool = Field(default=False, alias="cnyAmountConfirmed", strict=True)
    requires_cny_confirmation: bool = Field(
        default=False, alias="requiresCnyConfirmation", strict=True
    )

    @field_validator("date", "amount", mode="before")
    @classmethod
    def normalize_unfinished_value(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("display_date", "description")
    @classmethod
    def strip_editing_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("amount", "original_amount")
    @classmethod
    def normalize_amount(cls, value: Decimal | None) -> Decimal | None:
        if value is not None:
            if value.as_tuple().exponent < -2:
                raise ValueError("amount supports at most two decimal places")
            return quantize_money(value)
        return value

    @field_validator("category")
    @classmethod
    def reject_subsidy(cls, value: ExpenseCategory) -> ExpenseCategory:
        if value is ExpenseCategory.SUBSIDY:
            raise ValueError("subsidy is calculated by the server")
        return value

    @field_validator("itinerary_file_ids", "payment_proof_file_ids", "hotel_bill_file_ids")
    @classmethod
    def normalize_itinerary_ids(cls, values: list[str]) -> list[str]:
        result = [value.strip() for value in values]
        if any(not value or len(value) > 36 for value in result) or len(result) != len(set(result)):
            raise ValueError("proof file ids must contain unique valid file ids")
        return result

    source_file_id: str | None = Field(
        default=None,
        alias="sourceFileId",
        min_length=1,
        max_length=36,
    )

    @field_validator("source_file_id")
    @classmethod
    def normalize_source_file_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("sourceFileId must not be blank")
        return normalized

    @model_validator(mode="after")
    def normalize_itinerary_requirement(self) -> ReimbursementDraftExpenseItemInput:
        if self.transport_type is None and self.requires_itinerary:
            self.transport_type = "ride_hailing"
        self.requires_itinerary = self.transport_type == "ride_hailing"
        return self


class ReimbursementDraftInput(ExcelGenerateRequest):
    """Editable values whose authoritative result is recalculated by the server."""

    ocr_disposition_version: Literal[1] = Field(default=1, alias="ocrDispositionVersion")
    company_value: str = Field(default="", alias="companyValue", max_length=2048)
    accounting_source_verified: bool = Field(
        default=False, alias="accountingSourceVerified", strict=True
    )
    budget_code_value: str = Field(default="", alias="budgetCodeValue", max_length=2048)
    project: (
        Annotated[SelectedProjectInput | BudgetProjectInput, Field(discriminator="mode")] | None
    ) = None
    editing_state: ReimbursementEditingStateInput | None = Field(default=None, alias="editingState")
    items: list[ReimbursementDraftExpenseItemInput] = Field(
        max_length=MAX_EXPENSE_ITEMS_HARD_LIMIT,
    )
    dismissed_ocr_file_ids: list[str] = Field(
        default_factory=list,
        alias="dismissedOcrFileIds",
        max_length=MAX_EXPENSE_ITEMS_HARD_LIMIT,
    )

    @field_validator("company_value", "budget_code_value")
    @classmethod
    def normalize_oa_option_value(cls, value: str) -> str:
        normalized = value.strip()
        return normalized

    @field_validator("dismissed_ocr_file_ids")
    @classmethod
    def normalize_dismissed_ocr_file_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > 36 for value in normalized):
            raise ValueError("dismissedOcrFileIds contains an invalid file id")
        return normalized

    @model_validator(mode="after")
    def validate_ocr_file_dispositions(self) -> ReimbursementDraftInput:
        source_ids = [item.source_file_id for item in self.items if item.source_file_id is not None]
        dismissed_ids = self.dismissed_ocr_file_ids
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("sourceFileId values must be unique within a draft")
        if len(dismissed_ids) != len(set(dismissed_ids)):
            raise ValueError("dismissedOcrFileIds values must be unique within a draft")
        if set(source_ids).intersection(dismissed_ids):
            raise ValueError("an OCR file cannot be both linked and dismissed")
        return self


class CreateReimbursementDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    expected_revision: int = Field(alias="expectedRevision", ge=0, le=0, strict=True)
    input: ReimbursementDraftInput


class UpdateReimbursementDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    expected_revision: int = Field(alias="expectedRevision", ge=1, strict=True)
    input: ReimbursementDraftInput


class DraftRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    expected_revision: int = Field(alias="expectedRevision", ge=1, strict=True)


class TravelApprovalQueryWindowInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_date: StrictCalendarDate = Field(alias="from")
    to_date: StrictCalendarDate = Field(alias="to")


class RelatedApprovalSelectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    process_instance_id: str = Field(
        alias="processInstanceId",
        min_length=1,
        max_length=128,
    )
    profile_key: str = Field(alias="profileKey", min_length=1, max_length=64)
    query_window: TravelApprovalQueryWindowInput = Field(alias="queryWindow")

    @field_validator("process_instance_id", "profile_key")
    @classmethod
    def normalize_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("identifier must not be blank")
        return normalized


class ReplaceRelatedApprovalsRequest(DraftRevisionRequest):
    selections: list[RelatedApprovalSelectionInput] = Field(
        max_length=MAX_RELATED_APPROVALS,
    )

    @model_validator(mode="after")
    def reject_duplicate_instances(self) -> ReplaceRelatedApprovalsRequest:
        instance_ids = [item.process_instance_id for item in self.selections]
        if len(instance_ids) != len(set(instance_ids)):
            raise ValueError("related approval instances must be unique")
        return self

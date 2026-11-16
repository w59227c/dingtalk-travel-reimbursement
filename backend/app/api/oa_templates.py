from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.integrations.dingtalk.workflow import DingTalkWorkflowClient, FormOption
from app.schemas.common import success
from app.services.oa_template_profiles import (
    TravelProfileConfirmation,
    TravelProfileInspection,
    confirm_template_catalog,
    current_template_catalog,
    inspect_template_catalog,
)
from app.services.sessions import CurrentSession, require_admin, require_admin_csrf

router = APIRouter(tags=["oa-template-administration"])


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ProcessCodeValue(StrictRequest):
    process_code: str = Field(
        alias="processCode",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )

    @field_validator("process_code")
    @classmethod
    def normalize_process_code(cls, value: str) -> str:
        return value.strip()


class TravelProfileHeader(ProcessCodeValue):
    profile_key: str = Field(
        alias="profileKey",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    display_name: str = Field(alias="displayName", min_length=1, max_length=128)

    @field_validator("profile_key", "display_name")
    @classmethod
    def normalize_header_text(cls, value: str) -> str:
        return value.strip()

    def as_inspection(self) -> TravelProfileInspection:
        return TravelProfileInspection(
            profile_key=self.profile_key,
            display_name=self.display_name,
            process_code=self.process_code,
        )


class CatalogInspectionRequest(StrictRequest):
    reimbursement_process_code: str = Field(
        alias="reimbursementProcessCode",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    travel_profiles: list[TravelProfileHeader] = Field(
        alias="travelProfiles",
        min_length=1,
        max_length=20,
    )

    @field_validator("reimbursement_process_code")
    @classmethod
    def normalize_reimbursement_process_code(cls, value: str) -> str:
        return value.strip()


class TemplateMappingsWrite(StrictRequest):
    company: str = Field(min_length=1, max_length=128)
    budget_code: str = Field(alias="budgetCode", min_length=1, max_length=128)
    travel_type: str = Field(alias="travelType", min_length=1, max_length=128)
    start_date: str = Field(alias="startDate", min_length=1, max_length=128)
    end_date: str = Field(alias="endDate", min_length=1, max_length=128)
    duration_days: str = Field(alias="durationDays", min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=128)
    reimbursement_amount: str = Field(alias="reimbursementAmount", min_length=1, max_length=128)
    related_approvals: str = Field(alias="relatedApprovals", min_length=1, max_length=128)
    attachments: str = Field(min_length=1, max_length=128)
    reimbursement_employee: str | None = Field(
        default=None, alias="reimbursementEmployee", min_length=1, max_length=128,
    )
    reimbursement_department: str | None = Field(
        default=None, alias="reimbursementDepartment", min_length=1, max_length=128,
    )

    @field_validator("*")
    @classmethod
    def normalize_component_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("component id must not be blank")
        return normalized

    def as_mapping(self) -> dict[str, str]:
        mappings = {
            "company": self.company,
            "budgetCode": self.budget_code,
            "travelType": self.travel_type,
            "startDate": self.start_date,
            "endDate": self.end_date,
            "durationDays": self.duration_days,
            "description": self.description,
            "reimbursementAmount": self.reimbursement_amount,
            "relatedApprovals": self.related_approvals,
            "attachments": self.attachments,
        }
        if self.reimbursement_employee is not None:
            mappings["reimbursementEmployee"] = self.reimbursement_employee
        if self.reimbursement_department is not None:
            mappings["reimbursementDepartment"] = self.reimbursement_department
        return mappings


class TravelMappingsWrite(StrictRequest):
    start_date: str = Field(alias="startDate", min_length=1, max_length=128)
    end_date: str = Field(alias="endDate", min_length=1, max_length=128)
    company: str | None = Field(default=None, min_length=1, max_length=128)
    budget_code: str | None = Field(default=None, alias="budgetCode", min_length=1, max_length=128)
    travel_type: str | None = Field(default=None, alias="travelType", min_length=1, max_length=128)

    @field_validator("*")
    @classmethod
    def normalize_component_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("component id must not be blank")
        return normalized

    def as_mapping(self) -> dict[str, str]:
        mappings = {"startDate": self.start_date, "endDate": self.end_date}
        if self.company is not None:
            mappings["company"] = self.company
        if self.budget_code is not None:
            mappings["budgetCode"] = self.budget_code
        if self.travel_type is not None:
            mappings["travelType"] = self.travel_type
        return mappings


class FormOptionWrite(StrictRequest):
    value: str = Field(min_length=1, max_length=1024)
    label: str = Field(min_length=1, max_length=1024)
    key: str | None = Field(default=None, min_length=1, max_length=1024)

    @field_validator("value", "label", "key")
    @classmethod
    def normalize_option_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    def as_option(self) -> FormOption:
        return FormOption(value=self.value, label=self.label, key=self.key)


class ReimbursementCatalogConfirmation(ProcessCodeValue):
    schema_fingerprint: str = Field(
        alias="schemaFingerprint",
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    mappings: TemplateMappingsWrite


class TravelCatalogConfirmation(TravelProfileHeader):
    schema_fingerprint: str = Field(
        alias="schemaFingerprint",
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    mappings: TravelMappingsWrite
    travel_type_option: FormOptionWrite = Field(alias="travelTypeOption")
    travel_type_mappings: dict[str, FormOptionWrite] | None = Field(
        default=None, alias="travelTypeMappings"
    )

    def as_confirmation(self) -> TravelProfileConfirmation:
        return TravelProfileConfirmation(
            profile_key=self.profile_key,
            display_name=self.display_name,
            process_code=self.process_code,
            schema_fingerprint=self.schema_fingerprint,
            mappings=self.mappings.as_mapping(),
            travel_type_option=self.travel_type_option.as_option(),
            travel_type_mappings=(
                {key: value.as_option() for key, value in self.travel_type_mappings.items()}
                if self.travel_type_mappings is not None
                else None
            ),
        )


class CatalogConfirmationRequest(StrictRequest):
    expected_config_version: int | None = Field(
        alias="expectedConfigVersion",
        ge=1,
    )
    reimbursement: ReimbursementCatalogConfirmation
    travel_profiles: list[TravelCatalogConfirmation] = Field(
        alias="travelProfiles",
        min_length=1,
        max_length=20,
    )


@router.post("/admin/oa/templates/catalog/inspect")
async def inspect_catalog(
    body: CatalogInspectionRequest,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    _current: Annotated[CurrentSession, Depends(require_admin_csrf)],
) -> dict[str, object]:
    workflow: DingTalkWorkflowClient = request.app.state.dingtalk_workflow
    # Authentication shares this request-scoped session. End that read
    # transaction before waiting on DingTalk; catalog reads begin afterwards.
    database.rollback()
    inspected = await inspect_template_catalog(
        database,
        workflow,
        reimbursement_process_code=body.reimbursement_process_code,
        travel_profiles=[item.as_inspection() for item in body.travel_profiles],
    )
    return success(inspected)


@router.get("/admin/oa/templates/catalog")
def get_catalog(
    database: Annotated[Session, Depends(get_db)],
    _current: Annotated[CurrentSession, Depends(require_admin)],
) -> dict[str, object]:
    return success(current_template_catalog(database))


@router.put("/admin/oa/templates/catalog")
async def confirm_catalog(
    body: CatalogConfirmationRequest,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    current: Annotated[CurrentSession, Depends(require_admin_csrf)],
) -> dict[str, object]:
    workflow: DingTalkWorkflowClient = request.app.state.dingtalk_workflow
    administrator_user_id = current.record.dingtalk_user_id
    # Keep every remote schema read outside the final catalog CAS transaction.
    database.rollback()
    catalog = await confirm_template_catalog(
        database,
        workflow,
        reimbursement_process_code=body.reimbursement.process_code,
        expected_config_version=body.expected_config_version,
        reimbursement_schema_fingerprint=body.reimbursement.schema_fingerprint,
        reimbursement_mappings=body.reimbursement.mappings.as_mapping(),
        travel_profiles=[item.as_confirmation() for item in body.travel_profiles],
        administrator_user_id=administrator_user_id,
    )
    return success(catalog)

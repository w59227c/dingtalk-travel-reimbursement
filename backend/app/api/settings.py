from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.domain.subsidy import MAX_DAILY_SUBSIDY, TripType
from app.schemas.common import success
from app.schemas.primitives import DecimalString
from app.services.application_settings import (
    MAX_ADDITIONAL_ADMINS,
    MAX_APP_TITLE_LENGTH,
    get_additional_admin_ids,
    get_expense_settings,
    update_expense_settings,
)
from app.services.sessions import (
    CurrentSession,
    get_current_session,
    require_admin,
    require_admin_csrf,
)

router = APIRouter(tags=["settings"])


class SubsidyRatesWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    business: DecimalString = Field(ge=0, le=MAX_DAILY_SUBSIDY)
    short_term_project: DecimalString = Field(ge=0, le=MAX_DAILY_SUBSIDY)
    long_term_project: DecimalString = Field(ge=0, le=MAX_DAILY_SUBSIDY)
    same_city_project: DecimalString = Field(ge=0, le=MAX_DAILY_SUBSIDY)
    short_term_internal: DecimalString = Field(ge=0, le=MAX_DAILY_SUBSIDY)
    long_term_internal: DecimalString = Field(ge=0, le=MAX_DAILY_SUBSIDY)
    short_term_overseas: DecimalString = Field(ge=0, le=MAX_DAILY_SUBSIDY)
    long_term_overseas: DecimalString = Field(ge=0, le=MAX_DAILY_SUBSIDY)

    @field_validator(
        "business",
        "short_term_project",
        "long_term_project",
        "same_city_project",
        "short_term_internal",
        "long_term_internal",
        "short_term_overseas",
        "long_term_overseas",
    )
    @classmethod
    def validate_money_scale(cls, value: Decimal) -> Decimal:
        if value.as_tuple().exponent < -2:
            raise ValueError("subsidy rates support at most two decimal places")
        return value.quantize(Decimal("0.01"))

    def as_domain_mapping(self) -> dict[TripType, Decimal]:
        return {
            TripType.BUSINESS: self.business,
            TripType.SHORT_TERM_PROJECT: self.short_term_project,
            TripType.LONG_TERM_PROJECT: self.long_term_project,
            TripType.SAME_CITY_PROJECT: self.same_city_project,
            TripType.SHORT_TERM_INTERNAL: self.short_term_internal,
            TripType.LONG_TERM_INTERNAL: self.long_term_internal,
            TripType.SHORT_TERM_OVERSEAS: self.short_term_overseas,
            TripType.LONG_TERM_OVERSEAS: self.long_term_overseas,
        }


class SettingsWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    subsidy_rates: SubsidyRatesWrite = Field(alias="subsidyRates")
    calculation_mode: Literal["half_day_12"] = Field(alias="calculationMode")
    app_title: str = Field(alias="appTitle", min_length=1, max_length=MAX_APP_TITLE_LENGTH)
    admin_user_ids: list[str] = Field(alias="adminUserIds", max_length=MAX_ADDITIONAL_ADMINS)

    @field_validator("app_title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("invalid application title")
        return value

    @field_validator("admin_user_ids")
    @classmethod
    def normalize_admin_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if len(set(normalized)) != len(normalized) or any(
            not value or len(value) > 128 or "," in value or any(ord(char) < 33 for char in value)
            for value in normalized
        ):
            raise ValueError("invalid administrator userId")
        return normalized


@router.get("/settings")
def get_application_settings(
    database: Annotated[Session, Depends(get_db)],
    _current: Annotated[CurrentSession, Depends(get_current_session)],
) -> dict[str, object]:
    return success(get_expense_settings(database).as_api_dict())


@router.get("/admin/settings")
def get_admin_application_settings(
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    _current: Annotated[CurrentSession, Depends(require_admin)],
) -> dict[str, object]:
    settings = get_expense_settings(database).as_api_dict()
    return success(
        settings
        | {
            "adminUserIds": sorted(get_additional_admin_ids(database)),
            "environmentAdminUserIds": sorted(request.app.state.settings.admin_ids),
        }
    )


@router.put("/admin/settings")
def update_application_settings(
    body: SettingsWrite,
    request: Request,
    database: Annotated[Session, Depends(get_db)],
    _current: Annotated[CurrentSession, Depends(require_admin_csrf)],
) -> dict[str, object]:
    settings = update_expense_settings(
        database,
        daily_rates=body.subsidy_rates.as_domain_mapping(),
        calculation_mode=body.calculation_mode,
        app_title=body.app_title,
        additional_admin_ids=tuple(body.admin_user_ids),
    )
    return success(
        settings.as_api_dict()
        | {
            "adminUserIds": sorted(get_additional_admin_ids(database)),
            "environmentAdminUserIds": sorted(request.app.state.settings.admin_ids),
        }
    )

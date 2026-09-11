from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.core.errors import ApiError
from app.domain.money import MONEY_QUANTUM
from app.domain.subsidy import MAX_DAILY_SUBSIDY, TripType
from app.models.setting import Setting

logger = logging.getLogger(__name__)

CALCULATION_MODE_KEY = "calculation_mode"
DEFAULT_CALCULATION_MODE = "half_day_12"
APP_TITLE_KEY = "app_title"
ADMIN_USER_IDS_KEY = "admin_user_ids"
DEFAULT_APP_TITLE = "智能差旅费报销申请"
MAX_APP_TITLE_LENGTH = 40
MAX_ADDITIONAL_ADMINS = 50

RATE_KEYS: dict[TripType, str] = {
    TripType.BUSINESS: "subsidy_business_per_day",
    TripType.SHORT_TERM_PROJECT: "subsidy_short_term_project_per_day",
    TripType.LONG_TERM_PROJECT: "subsidy_long_term_project_per_day",
    TripType.SAME_CITY_PROJECT: "subsidy_same_city_project_per_day",
    TripType.INTERNAL: "subsidy_internal_per_day",
}
DEFAULT_RATES: dict[TripType, Decimal] = {
    TripType.BUSINESS: Decimal("100.00"),
    TripType.SHORT_TERM_PROJECT: Decimal("100.00"),
    TripType.LONG_TERM_PROJECT: Decimal("150.00"),
    TripType.SAME_CITY_PROJECT: Decimal("50.00"),
    TripType.INTERNAL: Decimal("100.00"),
}


@dataclass(frozen=True, slots=True)
class ExpenseSettings:
    daily_rates: Mapping[TripType, Decimal]
    calculation_mode: str
    app_title: str

    def daily_rate_for(self, trip_type: TripType) -> Decimal:
        return self.daily_rates[trip_type]

    def as_api_dict(self) -> dict[str, object]:
        return {
            "subsidyRates": {
                trip_type.value: format(self.daily_rates[trip_type], ".2f")
                for trip_type in RATE_KEYS
            },
            "calculationMode": self.calculation_mode,
            "appTitle": self.app_title,
        }


def ensure_expense_setting_rows(database: Session) -> dict[str, Setting]:
    """Seed the complete set of current application settings."""

    changed = False
    rows: dict[str, Setting] = {}
    for trip_type, key in RATE_KEYS.items():
        row = database.get(Setting, key)
        if row is None:
            row = Setting(key=key, value=format(DEFAULT_RATES[trip_type], ".2f"))
            database.add(row)
            changed = True
        rows[key] = row

    mode = database.get(Setting, CALCULATION_MODE_KEY)
    if mode is None:
        mode = Setting(key=CALCULATION_MODE_KEY, value=DEFAULT_CALCULATION_MODE)
        database.add(mode)
        changed = True
    rows[CALCULATION_MODE_KEY] = mode
    title = database.get(Setting, APP_TITLE_KEY)
    if title is None:
        title = Setting(key=APP_TITLE_KEY, value=DEFAULT_APP_TITLE)
        database.add(title)
        changed = True
    rows[APP_TITLE_KEY] = title
    admins = database.get(Setting, ADMIN_USER_IDS_KEY)
    if admins is None:
        admins = Setting(key=ADMIN_USER_IDS_KEY, value="[]")
        database.add(admins)
        changed = True
    rows[ADMIN_USER_IDS_KEY] = admins
    if changed:
        database.commit()
    return rows


def _configuration_error(key: str) -> ApiError:
    logger.error("Invalid stored expense setting: %s", key)
    return ApiError(
        "INVALID_SYSTEM_CONFIGURATION",
        "系统补助配置无效，请联系管理员",
        500,
    )


def _validated_rate(row: Setting, key: str, *, allow_zero: bool = False) -> Decimal:
    try:
        amount = Decimal(row.value)
    except (InvalidOperation, ValueError):
        raise _configuration_error(key) from None
    if (
        not amount.is_finite()
        or amount < 0
        or (amount == 0 and not allow_zero)
        or amount > MAX_DAILY_SUBSIDY
        or amount.as_tuple().exponent < -2
    ):
        raise _configuration_error(key)
    return amount.quantize(MONEY_QUANTUM)


def get_expense_settings(database: Session) -> ExpenseSettings:
    rows = ensure_expense_setting_rows(database)
    rates = {trip_type: _validated_rate(rows[key], key) for trip_type, key in RATE_KEYS.items()}
    mode = rows[CALCULATION_MODE_KEY]
    if mode.value != DEFAULT_CALCULATION_MODE:
        raise _configuration_error(CALCULATION_MODE_KEY)
    return ExpenseSettings(
        daily_rates=rates,
        calculation_mode=mode.value,
        app_title=_validated_app_title(rows[APP_TITLE_KEY]),
    )


def _validated_app_title(row: Setting) -> str:
    title = row.value.strip()
    if not title or len(title) > MAX_APP_TITLE_LENGTH or any(ord(char) < 32 for char in title):
        raise _configuration_error(APP_TITLE_KEY)
    return title


def get_additional_admin_ids(database: Session) -> frozenset[str]:
    row = database.get(Setting, ADMIN_USER_IDS_KEY)
    if row is None:
        return frozenset()
    try:
        values = json.loads(row.value)
        if not isinstance(values, list) or len(values) > MAX_ADDITIONAL_ADMINS:
            raise ValueError
        normalized = tuple(str(value).strip() for value in values)
        if any(
            not value or len(value) > 128 or "," in value or any(ord(char) < 33 for char in value)
            for value in normalized
        ):
            raise ValueError
        if len(set(normalized)) != len(normalized):
            raise ValueError
        return frozenset(normalized)
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.error("Invalid stored administrator configuration")
        return frozenset()


def get_app_title(database: Session) -> str:
    row = database.get(Setting, APP_TITLE_KEY)
    return DEFAULT_APP_TITLE if row is None else _validated_app_title(row)


def update_expense_settings(
    database: Session,
    *,
    daily_rates: Mapping[TripType, Decimal],
    calculation_mode: str,
    app_title: str,
    additional_admin_ids: tuple[str, ...],
) -> ExpenseSettings:
    rows = ensure_expense_setting_rows(database)
    for trip_type, key in RATE_KEYS.items():
        rows[key].value = format(daily_rates[trip_type], ".2f")
    rows[CALCULATION_MODE_KEY].value = calculation_mode
    rows[APP_TITLE_KEY].value = app_title
    rows[ADMIN_USER_IDS_KEY].value = json.dumps(
        sorted(set(additional_admin_ids)), ensure_ascii=False, separators=(",", ":")
    )
    database.commit()
    return get_expense_settings(database)

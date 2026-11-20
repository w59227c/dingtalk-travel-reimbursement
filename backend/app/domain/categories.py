from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum


class ExpenseCategory(StrEnum):
    AIRFARE = "airfare"
    RAIL_FARE = "rail_fare"
    LOCAL_TRANSPORT = "local_transport"
    LODGING = "lodging"
    SUBSIDY = "subsidy"
    OFFICE = "office"
    HOSPITALITY = "hospitality"
    COMMUNICATIONS = "communications"
    EMPLOYEE_WELFARE = "employee_welfare"
    CONSULTING = "consulting"
    ADVERTISING = "advertising"
    LEASING = "leasing"
    PROPERTY_MANAGEMENT = "property_management"
    UTILITIES = "utilities"
    LABOR_SERVICE = "labor_service"
    CONFERENCE = "conference"
    OTHER = "other"
    EXPRESS = "express"


@dataclass(frozen=True, slots=True)
class ExpenseCategoryMetadata:
    id: ExpenseCategory
    name: str
    order: int
    manual_selectable: bool = True

    def as_api_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["id"] = self.id.value
        value["manualSelectable"] = value.pop("manual_selectable")
        return value


EXPENSE_CATEGORIES: tuple[ExpenseCategoryMetadata, ...] = (
    ExpenseCategoryMetadata(ExpenseCategory.AIRFARE, "飞机票", 1),
    ExpenseCategoryMetadata(ExpenseCategory.RAIL_FARE, "火车票", 2),
    ExpenseCategoryMetadata(ExpenseCategory.LOCAL_TRANSPORT, "市内交通费", 3),
    ExpenseCategoryMetadata(ExpenseCategory.LODGING, "住宿费", 4),
    ExpenseCategoryMetadata(ExpenseCategory.SUBSIDY, "出差补助", 5, False),
    ExpenseCategoryMetadata(ExpenseCategory.OFFICE, "办公费", 6),
    ExpenseCategoryMetadata(ExpenseCategory.HOSPITALITY, "招待费", 7),
    ExpenseCategoryMetadata(ExpenseCategory.COMMUNICATIONS, "通讯费", 8),
    ExpenseCategoryMetadata(ExpenseCategory.EMPLOYEE_WELFARE, "福利费", 9),
    ExpenseCategoryMetadata(ExpenseCategory.CONSULTING, "咨询费", 10),
    ExpenseCategoryMetadata(ExpenseCategory.ADVERTISING, "广告费", 11),
    ExpenseCategoryMetadata(ExpenseCategory.LEASING, "租赁费", 12),
    ExpenseCategoryMetadata(ExpenseCategory.PROPERTY_MANAGEMENT, "物业费", 13),
    ExpenseCategoryMetadata(ExpenseCategory.UTILITIES, "水电费", 14),
    ExpenseCategoryMetadata(ExpenseCategory.LABOR_SERVICE, "劳务费", 15),
    ExpenseCategoryMetadata(ExpenseCategory.CONFERENCE, "会议费", 16),
    ExpenseCategoryMetadata(ExpenseCategory.OTHER, "其他", 17),
)

CATEGORY_BY_ID = {item.id: item for item in EXPENSE_CATEGORIES}
CATEGORY_BY_ID[ExpenseCategory.EXPRESS] = ExpenseCategoryMetadata(
    ExpenseCategory.EXPRESS, "快递费", 18
)

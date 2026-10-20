from __future__ import annotations

from app.domain.categories import ExpenseCategory
from app.ocr.types import ReceiptKeywordRule

# These are category-assignment hints, not the structural signals used to
# recognize a train ticket or an invoice. They are seeded into the database so
# administrators can change them without changing application code.
DEFAULT_RECEIPT_KEYWORD_RULES: tuple[ReceiptKeywordRule, ...] = (
    ReceiptKeywordRule("酒店", ExpenseCategory.LODGING),
    ReceiptKeywordRule("宾馆", ExpenseCategory.LODGING),
    ReceiptKeywordRule("住宿", ExpenseCategory.LODGING),
    ReceiptKeywordRule("房费", ExpenseCategory.LODGING),
    ReceiptKeywordRule("客房", ExpenseCategory.LODGING),
    ReceiptKeywordRule("航空", ExpenseCategory.AIRFARE),
    ReceiptKeywordRule("飞机", ExpenseCategory.AIRFARE),
    ReceiptKeywordRule("民航", ExpenseCategory.AIRFARE),
    ReceiptKeywordRule("机场", ExpenseCategory.AIRFARE),
    ReceiptKeywordRule("燃油附加费", ExpenseCategory.AIRFARE),
    ReceiptKeywordRule("铁路", ExpenseCategory.RAIL_FARE),
    ReceiptKeywordRule("火车", ExpenseCategory.RAIL_FARE),
    ReceiptKeywordRule("高铁", ExpenseCategory.RAIL_FARE),
    ReceiptKeywordRule("动车", ExpenseCategory.RAIL_FARE),
    ReceiptKeywordRule("出租汽车", ExpenseCategory.LOCAL_TRANSPORT, "taxi"),
    ReceiptKeywordRule("出租车", ExpenseCategory.LOCAL_TRANSPORT, "taxi"),
    ReceiptKeywordRule("公路客运", ExpenseCategory.LOCAL_TRANSPORT),
    ReceiptKeywordRule("道路旅客运输", ExpenseCategory.LOCAL_TRANSPORT),
    ReceiptKeywordRule("汽车客运", ExpenseCategory.LOCAL_TRANSPORT),
    ReceiptKeywordRule("客运服务费", ExpenseCategory.LOCAL_TRANSPORT),
    ReceiptKeywordRule("客运服务", ExpenseCategory.LOCAL_TRANSPORT),
    ReceiptKeywordRule("网约车", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
    ReceiptKeywordRule("网络预约出租", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
    ReceiptKeywordRule("滴滴出行", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
    ReceiptKeywordRule("滴滴科技", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
    ReceiptKeywordRule("滴滴畅行", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
    ReceiptKeywordRule("滴滴快车", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
    ReceiptKeywordRule("滴滴专车", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
    ReceiptKeywordRule("曹操出行", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
    ReceiptKeywordRule("吉利优行电子科技", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
    ReceiptKeywordRule("t3出行", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
    ReceiptKeywordRule("首汽约车", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
    ReceiptKeywordRule("花小猪", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
    ReceiptKeywordRule("高德打车", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
    ReceiptKeywordRule("南京领行科技", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
    ReceiptKeywordRule("长燃智享", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
    ReceiptKeywordRule("享道出行", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
    ReceiptKeywordRule("郑州哈拜网络科技", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
    ReceiptKeywordRule("杭州携华网络科技", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
    ReceiptKeywordRule("收派服务", ExpenseCategory.EMPLOYEE_WELFARE),
    ReceiptKeywordRule("床品", ExpenseCategory.EMPLOYEE_WELFARE),
    ReceiptKeywordRule("床笠", ExpenseCategory.EMPLOYEE_WELFARE),
    ReceiptKeywordRule("床单", ExpenseCategory.EMPLOYEE_WELFARE),
    ReceiptKeywordRule("被套", ExpenseCategory.EMPLOYEE_WELFARE),
)

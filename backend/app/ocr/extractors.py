from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from app.domain.categories import ExpenseCategory
from app.domain.money import MAX_REIMBURSEMENT_AMOUNT, quantize_money
from app.ocr.keyword_defaults import DEFAULT_RECEIPT_KEYWORD_RULES
from app.ocr.types import OcrLine

_FULL_DATE = re.compile(
    r"(?<!\d)(?P<year>20\d{2})\s*[年./-]\s*(?P<month>0?[1-9]|1[0-2])\s*[月./-]\s*"
    r"(?P<day>0?[1-9]|[12]\d|3[01])\s*日?(?!\d)"
)
# PDF text layers sometimes concatenate the masked ID, date, and first route
# cell without spaces. Layout extraction may therefore see ``...5162026-07-01``;
# the row context makes that formatted date safe even though a digit precedes it.
_LAYOUT_FULL_DATE = re.compile(
    r"(?P<year>20\d{2})\s*[年./-]\s*(?P<month>0?[1-9]|1[0-2])\s*[月./-]\s*"
    r"(?P<day>0?[1-9]|[12]\d|3[01])\s*日?(?!\d)"
)
_SHORT_DATE = re.compile(
    r"(?<!\d)(?P<month>0?[1-9]|1[0-2])\s*月\s*(?P<day>0?[1-9]|[12]\d|3[01])\s*日(?!\d)"
)
_TRAIN_DEPARTURE_TIME = re.compile(r"(?<!\d)(?:[01]\d|2[0-3])\s*[:：]\s*[0-5]\d\s*开")
_MONEY = re.compile(r"(?:[¥￥]\s*)?(?<!\d)(\d{1,9}(?:,\d{3})*(?:\.\d{1,2})?)(?!\d)")
_CURRENCY_MONEY = re.compile(r"[¥￥]\s*(\d{1,9}(?:,\d{3})*(?:\.\d{1,2})?)")
_CHINESE_MONEY = re.compile(r"[零〇壹贰叁肆伍陆柒捌玖拾佰仟万亿元圆角分整正]+")
_ROUTE = re.compile(
    r"([\u4e00-\u9fff]{2,12}(?:站|南|北|东|西)?)\s*(?:[-—–→至])\s*"
    r"([\u4e00-\u9fff]{2,12}(?:站|南|北|东|西)?)"
)
_TRAIN_NUMBER = re.compile(r"^[GDCZTK]\d{1,6}$", re.IGNORECASE)
_PINYIN_STATION = re.compile(r"^[A-Za-z][A-Za-z\s]{3,60}$")
PASSENGER_ROUTE_PREFIX = "行程路线："
PASSENGER_OCCURRENCE_DATE_PREFIX = "发生日期："
_LAYOUT_COLUMN_GAP = re.compile(r"\s{4,}")
_ROUTE_FORBIDDEN_TEXT = ("价税合计", "开票日期", "发票号码", "项目名称")
_PASSENGER_ROW_HEADERS = ("出行日期", "出发地", "到达地", "交通工具类型")
_PASSENGER_TRANSPORT_VALUES = tuple(
    rule.keyword
    for rule in DEFAULT_RECEIPT_KEYWORD_RULES
    if rule.category
    in {ExpenseCategory.AIRFARE, ExpenseCategory.RAIL_FARE, ExpenseCategory.LOCAL_TRANSPORT}
)
# Validate a flattened table's vehicle cell, without assigning an expense category.
_PASSENGER_VEHICLE_VALUE = re.compile(
    r"航空|飞机|民航|铁路|火车|高铁|动车(?:组)?|(?:网络预约)?出租汽车|出租车|网约车"
    r"|道路旅客运输|公路客运|汽车客运"
)
_TRANSPORT_TAX_ITEM = re.compile(r"[*＊]\s*交通运输服务\s*[*＊]")
_PASSENGER_ROW_NON_LOCATIONS = frozenset(
    {
        "无",
        "惠选",
        "其他",
        "出租车",
        "出租汽车",
        "网约车",
        "铁路",
        "飞机",
    }
)
_ROUTE_PAREN_CONTINUATION = re.compile(r"[\u4e00-\u9fffA-Za-z0-9·-]{1,12}[)）]")


def normalized_lines(lines: list[OcrLine]) -> list[OcrLine]:
    result: list[OcrLine] = []
    for line in lines:
        text = " ".join(line.text.replace("\x00", " ").split()).strip()
        if text:
            result.append(OcrLine(text=text[:1000], confidence=max(0.0, min(1.0, line.confidence))))
    return result


def average_confidence(lines: list[OcrLine]) -> float:
    values = [line.confidence for line in lines if line.text.strip()]
    return sum(values) / len(values) if values else 0.0


def _date_from_match(match: re.Match[str], reference_year: int) -> date | None:
    try:
        return date(
            int(match.groupdict().get("year") or reference_year),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError:
        return None


def _dates_in_text(text: str, reference_year: int) -> list[date]:
    result: list[date] = []
    for pattern in (_FULL_DATE, _SHORT_DATE):
        for match in pattern.finditer(text):
            parsed = _date_from_match(match, reference_year)
            if parsed is not None:
                result.append(parsed)
    return result


def extract_date(
    lines: list[OcrLine],
    reference_year: int,
    preferred_labels: tuple[str, ...] = (),
) -> date | None:
    # Labels are a semantic priority list, not merely a hint that a line is
    # preferred. OCR line order must not make an invoice date win over a later
    # travel/boarding date.
    for label in preferred_labels:
        for line in lines:
            if label not in line.text:
                continue
            dates = _dates_in_text(line.text, reference_year)
            if dates:
                return dates[0]

    # Only after every labeled priority has been exhausted may an unlabeled
    # date be used as a fallback.
    for line in lines:
        if any(label in line.text for label in preferred_labels):
            continue
        dates = _dates_in_text(line.text, reference_year)
        if dates:
            return dates[0]
    return None


def extract_train_travel_date(lines: list[OcrLine], reference_year: int) -> date | None:
    """Extract a train's occurrence date without mistaking the invoice date for travel."""

    travel_labels = ("出行日期", "乘车日期", "开车时间")
    for label in travel_labels:
        for line in lines:
            if label not in line.text:
                continue
            dates = _dates_in_text(line.text, reference_year)
            if dates:
                return dates[0]

    non_invoice_lines = [line for line in lines if "开票日期" not in line.text]
    for line in non_invoice_lines:
        if not _TRAIN_DEPARTURE_TIME.search(line.text):
            continue
        dates = _dates_in_text(line.text, reference_year)
        if dates:
            return dates[0]

    # Text-layer extraction often separates the boarding date from its time and
    # label. A single distinct non-invoice date is still unambiguous on a train
    # ticket; multiple candidates are left for manual review rather than guessed.
    candidates = {
        parsed for line in non_invoice_lines for parsed in _dates_in_text(line.text, reference_year)
    }
    if len(candidates) == 1:
        return next(iter(candidates))
    if candidates:
        return None

    # Some ticket images expose only their invoice date. Keep that as a final
    # fallback so OCR still supplies an editable date instead of inventing one.
    return extract_date(lines, reference_year, ("开票日期",))


def extract_passenger_occurrence_date(lines: list[OcrLine], reference_year: int) -> date | None:
    """Extract when passenger transport happened, using the invoice date only as fallback."""

    occurrence_labels = (
        PASSENGER_OCCURRENCE_DATE_PREFIX.removesuffix("："),
        "出行日期",
        "乘车日期",
    )
    for label in occurrence_labels:
        for line in lines:
            if label not in line.text:
                continue
            dates = _dates_in_text(line.text, reference_year)
            if dates:
                return dates[0]

    invoice_dates = {
        parsed
        for line in lines
        if "开票日期" in line.text
        for parsed in _dates_in_text(line.text, reference_year)
    }
    # Passenger-invoice text layers and OCR output commonly separate the table
    # heading from its data row. If there is exactly one date outside the
    # opening-date line, it is the only safe occurrence-date candidate. A PDF
    # text layer may also emit the same opening date again as an unlabeled line,
    # so exclude dates already established by labeled image OCR evidence.
    non_invoice_dates = {
        parsed
        for line in lines
        if "开票日期" not in line.text
        for parsed in _dates_in_text(line.text, reference_year)
        if parsed not in invoice_dates
    }
    if len(non_invoice_dates) == 1:
        return next(iter(non_invoice_dates))
    if non_invoice_dates:
        return None

    # Some invoices expose no trip date. Preserve the editable invoice date as
    # a final fallback instead of manufacturing an occurrence date.
    return extract_date(lines, reference_year, ("开票日期",))


def uses_invoice_date_as_occurrence(
    lines: list[OcrLine],
    reference_year: int,
    parsed_date: date | None,
    occurrence_labels: tuple[str, ...],
) -> bool:
    """Report when a transport date is only an editable invoice-date fallback."""

    if parsed_date is None:
        return False
    invoice_date: date | None = None
    for line in lines:
        if "开票日期" not in line.text:
            continue
        dates = _dates_in_text(line.text, reference_year)
        if dates:
            invoice_date = dates[0]
            break
    if invoice_date is None and any("开票日期" in line.text for line in lines):
        # Some PDF text layers emit all labels first, then their values. With
        # only one date and no explicit travel evidence below, provenance is
        # uncertain: flag it for review instead of treating it as confirmed travel.
        candidates = {
            value for line in lines for value in _dates_in_text(line.text, reference_year)
        }
        if len(candidates) == 1:
            invoice_date = next(iter(candidates))
    if invoice_date != parsed_date:
        return False
    for line in lines:
        if any(label in line.text for label in occurrence_labels):
            if _dates_in_text(line.text, reference_year):
                return False
        # A train ticket usually prints the boarding date beside a departure
        # time ending in "开", without a separate "乘车日期" label. When that
        # date happens to equal the invoice date, value comparison alone cannot
        # establish provenance; the departure-time pattern is explicit travel
        # evidence and must win over the invoice fallback warning.
        if (
            "开票日期" not in line.text
            and _TRAIN_DEPARTURE_TIME.search(line.text)
            and parsed_date in _dates_in_text(line.text, reference_year)
        ):
            return False
        if "开票日期" not in line.text and any(
            candidate != invoice_date for candidate in _dates_in_text(line.text, reference_year)
        ):
            return False
    return True


def _decimal(raw: str) -> Decimal | None:
    try:
        value = Decimal(raw.replace(",", ""))
        if not value.is_finite() or value < 0 or value > MAX_REIMBURSEMENT_AMOUNT:
            return None
        return quantize_money(value)
    except (InvalidOperation, ValueError):
        return None


def _money_candidates(text: str, *, require_currency: bool) -> list[Decimal]:
    pattern = _CURRENCY_MONEY if require_currency else _MONEY
    candidates: list[Decimal] = []
    for match in pattern.finditer(text):
        raw = match.group(1)
        # Bare long integers are usually invoice/phone/tax identifiers, not money.
        if not require_currency and "." not in raw and len(raw.replace(",", "")) > 6:
            continue
        value = _decimal(raw)
        if value is not None:
            candidates.append(value)
    return candidates


def extract_amount(
    lines: list[OcrLine],
    preferred_labels: tuple[str, ...],
    *,
    allow_currency_fallback: bool = True,
) -> Decimal | None:
    for label in preferred_labels:
        for line in lines:
            if label in line.text:
                values = _money_candidates(line.text, require_currency=False)
                if values:
                    return values[-1]
    if allow_currency_fallback:
        values: list[Decimal] = []
        for line in lines:
            values.extend(_money_candidates(line.text, require_currency=True))
        if values:
            return max(values)
    return None


def _labeled_amount(
    lines: list[OcrLine],
    labels: tuple[str, ...],
    *,
    stop_labels: tuple[str, ...] = (),
    excluded_text: tuple[str, ...] = (),
) -> Decimal | None:
    """Read the first monetary value following a label, bounded by later labels."""

    values = _labeled_amount_candidates(
        lines,
        labels,
        stop_labels=stop_labels,
        excluded_text=excluded_text,
    )
    return values[0] if values else None


def _labeled_amount_candidates(
    lines: list[OcrLine],
    labels: tuple[str, ...],
    *,
    stop_labels: tuple[str, ...] = (),
    excluded_text: tuple[str, ...] = (),
) -> list[Decimal]:
    """Read monetary values after a label, tolerating PDF glyph spacing."""

    for label in labels:
        for line in lines:
            compact_text = "".join(line.text.split())
            compact_label = "".join(label.split())
            if compact_label not in compact_text or any(
                "".join(value.split()) in compact_text for value in excluded_text
            ):
                continue
            suffix = compact_text.split(compact_label, 1)[1]
            compact_stops = ["".join(stop.split()) for stop in stop_labels]
            positions = [suffix.find(stop) for stop in compact_stops if stop in suffix]
            if positions:
                suffix = suffix[: min(positions)]
            values = _money_candidates(suffix, require_currency=False)
            if values:
                return values
    return []


_UPPER_DIGITS = {
    "零": 0,
    "〇": 0,
    "壹": 1,
    "贰": 2,
    "叁": 3,
    "肆": 4,
    "伍": 5,
    "陆": 6,
    "柒": 7,
    "捌": 8,
    "玖": 9,
}
_UPPER_SMALL_UNITS = {"拾": 10, "佰": 100, "仟": 1000}


def _uppercase_section(value: str) -> int | None:
    section = 0
    digit: int | None = None
    for character in value:
        if character in _UPPER_DIGITS:
            digit = _UPPER_DIGITS[character]
        elif character in _UPPER_SMALL_UNITS:
            section += (1 if digit is None else digit) * _UPPER_SMALL_UNITS[character]
            digit = None
        else:
            return None
    return section + (0 if digit is None else digit)


def _uppercase_integer(value: str) -> int | None:
    total = 0
    remainder = value
    if "亿" in remainder:
        billions, remainder = remainder.split("亿", 1)
        parsed = _uppercase_section(billions)
        if parsed is None:
            return None
        total += parsed * 100_000_000
    if "万" in remainder:
        ten_thousands, remainder = remainder.split("万", 1)
        parsed = _uppercase_section(ten_thousands)
        if parsed is None:
            return None
        total += parsed * 10_000
    parsed_remainder = _uppercase_section(remainder)
    return None if parsed_remainder is None else total + parsed_remainder


def _uppercase_money(value: str) -> Decimal | None:
    normalized = value.replace("圆", "元").replace("正", "整")
    if "元" not in normalized:
        return None
    integer_text, fraction_text = normalized.split("元", 1)
    integer = _uppercase_integer(integer_text) if integer_text else 0
    if integer is None:
        return None
    fraction = Decimal("0")
    angle = re.search(r"([零〇壹贰叁肆伍陆柒捌玖])角", fraction_text)
    cent = re.search(r"([零〇壹贰叁肆伍陆柒捌玖])分", fraction_text)
    if angle:
        fraction += Decimal(_UPPER_DIGITS[angle.group(1)]) / Decimal("10")
    if cent:
        fraction += Decimal(_UPPER_DIGITS[cent.group(1)]) / Decimal("100")
    try:
        return quantize_money(Decimal(integer) + fraction)
    except ValueError:
        return None


def _labeled_uppercase_amount(lines: list[OcrLine]) -> Decimal | None:
    for line in lines:
        if "价税合计" not in line.text or "大写" not in line.text:
            continue
        match = _CHINESE_MONEY.search(line.text.split("大写", 1)[1])
        if match:
            parsed = _uppercase_money(match.group())
            if parsed is not None:
                return parsed
    return None


def extract_invoice_amount(
    lines: list[OcrLine],
    qr_amount: Decimal | None = None,
) -> tuple[Decimal | None, tuple[str, ...]]:
    """Choose a reimbursement total and reconcile supplemental QR evidence.

    A legacy invoice QR amount may be tax-exclusive. Visible ``价税合计`` is
    therefore authoritative; QR evidence may confirm it or help distinguish a
    weak ``合计`` from tax and untaxed amounts, but never silently overwrites a
    conflicting visible total.
    """

    strong_total = _labeled_amount(
        lines,
        ("价税合计（小写）", "价税合计(小写)", "价税合计"),
        stop_labels=("税额",),
        excluded_text=("大写",),
    )
    if strong_total is None:
        strong_total = _labeled_uppercase_amount(lines)
    weak_values = _labeled_amount_candidates(
        lines,
        ("合计",),
        stop_labels=("税额",),
        excluded_text=("价税合计",),
    )
    weak_total = weak_values[0] if weak_values else None
    tax = _labeled_amount(lines, ("税额",))
    # Some electronic invoices place the untaxed amount and tax on one data
    # row below separate table headers, for example ``合 计 6.89¥ 0.21¥``.
    # The two bounded values are still explicit invoice evidence: first the
    # untaxed subtotal, then tax. A single weak ``合计`` remains insufficient.
    if tax is None and len(weak_values) == 2:
        tax = weak_values[1]
    derived_total = None
    if strong_total is None and weak_total is not None and tax is not None:
        try:
            derived_total = quantize_money(weak_total + tax)
        except ValueError:
            derived_total = None
    visible = strong_total if strong_total is not None else derived_total
    if visible is None and weak_total is None and tax is None:
        visible = extract_amount(lines, (), allow_currency_fallback=True)

    if qr_amount is None:
        return visible, ()
    if visible is None:
        return qr_amount, ("QR_AMOUNT_REQUIRES_REVIEW",)
    if visible == qr_amount:
        return visible, ()
    if tax is not None:
        if qr_amount + tax == visible:
            return visible, ()
        if visible + tax == qr_amount and strong_total is None:
            return qr_amount, ()
    return visible, ("QR_AMOUNT_MISMATCH",)


def extract_passenger_service_keywords(lines: list[OcrLine], keywords: tuple[str, ...]) -> set[str]:
    """Match configured words inside service cells, including wrapped PDF cells.

    Numeric columns can interrupt a project name in plain PDF text. A following
    fragment is joined only when it completes the same configured word, never
    by concatenating an unrelated route, company, or next invoice item.
    """
    texts = [
        re.sub(r"^(?:项目|商品|服务)名称\s*[:：]?\s*", "", line.text.strip())
        for line in lines
        if line.text.strip()
    ]
    matched: set[str] = set()
    for index, text in enumerate(texts):
        tax_item = _TRANSPORT_TAX_ITEM.search(text)
        if tax_item:
            cell = text[tax_item.end() :]
        elif any(label in text for label in ("服务", "客运")) and not any(
            label in text for label in ("公司", "发票", "出发地", "到达地", "备注", "名称")
        ):
            cell = text
        else:
            continue
        head = "".join(re.split(r"[\d¥￥%*＊:：]", cell, maxsplit=1)[0].split()).rstrip("+-−")
        if not head:
            continue
        for keyword in keywords:
            word = "".join(keyword.casefold().split())
            if not word:
                continue
            if word in head.casefold():
                matched.add(keyword)
                continue
            for length in range(2, len(word)):
                if not head.casefold().endswith(word[:length]):
                    continue
                remaining = word[length:]
                for continuation in texts[index + 1 : index + 4]:
                    fragment = (
                        "".join(re.split(r"[\d¥￥%*＊:：]", continuation, maxsplit=1)[0].split())
                        .rstrip("+-−")
                        .casefold()
                    )
                    if not fragment:
                        break
                    if fragment.startswith(remaining):
                        matched.add(keyword)
                        break
                    if not remaining.startswith(fragment):
                        break
                    remaining = remaining[len(fragment) :]
                    if not remaining:
                        matched.add(keyword)
                        break
    return matched


def extract_passenger_transport_type(
    lines: list[OcrLine],
    values: tuple[str, ...] | None = None,
) -> str | None:
    """Extract transport evidence from the invoice field instead of route/company text."""

    texts = [line.text.strip() for line in lines if line.text.strip()]
    for index, text in enumerate(texts):
        if "交通工具类型" not in text:
            continue
        suffix = text.split("交通工具类型", 1)[1].strip(" ：:")
        explicit_suffix = bool(suffix)
        header = " ".join(texts[max(0, index - 8) : index + 1])
        table = all(label in header for label in ("出发地", "到达地"))
        search_values = (
            [suffix] if explicit_suffix else _passenger_table_transport_cells(texts, index)
            if table else texts[index + 1 : index + 2]
        )
        matches: list[str] = []
        for candidate in search_values:
            if any(stop in candidate for stop in ("价税合计", "开票日期", "开票人")):
                break
            candidates = [candidate]
            for value in _PASSENGER_TRANSPORT_VALUES if values is None else values:
                if (
                    any(
                        value in field if explicit_suffix else value == field
                        for field in candidates
                    )
                    and value not in matches
                ):
                    matches.append(value)
        if matches:
            return " ".join(matches)
        return None
    return None


def _passenger_table_transport_cells(texts: list[str], header_index: int) -> list[str]:
    """Read the final cell only when a dated row has every expected column."""
    header = " ".join(texts[max(0, header_index - 8) : header_index + 1])
    expected = 4 if "等级" in header else 3  # origin, destination, optional grade, vehicle
    cells: list[str] = []
    boundaries = ("价税合计", "合计", "开票日期", "开票人", "购买方", "销售方", "项目名称")
    for row_index in range(header_index + 1, min(len(texts), header_index + 10)):
        text = texts[row_index]
        if any(boundary in text for boundary in boundaries):
            break
        match = _FULL_DATE.search(text)
        if match is None:
            continue
        values = text[match.end() :].split()
        if not values:
            for following in texts[row_index + 1 : row_index + expected + 4]:
                if any(boundary in following for boundary in boundaries) or _FULL_DATE.search(
                    following
                ):
                    break
                values.extend(following.split())
        if len(values) == expected and _PASSENGER_VEHICLE_VALUE.fullmatch(values[-1]):
            cells.append(values[-1])
    return cells


def _compact_layout_value(value: str) -> str:
    return "".join(value.replace("\x00", " ").split()).replace("|", "").replace("｜", "").strip()


def _layout_cells(raw_line: str) -> list[tuple[str, int]]:
    """Split a layout-text row while retaining each visual column start."""

    result: list[tuple[str, int]] = []
    cursor = 0
    for gap in _LAYOUT_COLUMN_GAP.finditer(raw_line):
        raw_cell = raw_line[cursor : gap.start()]
        if raw_cell.strip():
            leading = len(raw_cell) - len(raw_cell.lstrip())
            result.append((raw_cell.strip(), cursor + leading))
        cursor = gap.end()
    raw_cell = raw_line[cursor:]
    if raw_cell.strip():
        leading = len(raw_cell) - len(raw_cell.lstrip())
        result.append((raw_cell.strip(), cursor + leading))
    return result


def _valid_route_continuation(value: str) -> bool:
    return (
        1 <= len(value) <= 40
        and bool(re.search(r"[\u4e00-\u9fff]", value))
        and value not in _PASSENGER_ROW_NON_LOCATIONS
        and not any(forbidden in value for forbidden in _ROUTE_FORBIDDEN_TEXT)
        and _FULL_DATE.search(value) is None
    )


def _unclosed_parenthesis(value: str) -> bool:
    return value.count("(") > value.count(")") or value.count("（") > value.count("）")


def _valid_route_location(value: str) -> bool:
    return (
        2 <= len(value) <= 100
        and bool(re.search(r"[\u4e00-\u9fff]", value))
        and not any(forbidden in value for forbidden in _ROUTE_FORBIDDEN_TEXT)
    )


def extract_passenger_fields_from_layout(layout_text: str) -> tuple[date | None, str | None]:
    """Recover occurrence date and route from one validated passenger-invoice row."""

    raw_lines = layout_text.splitlines()
    for line_index, raw_line in enumerate(raw_lines):
        cells = _layout_cells(raw_line)
        for date_index, (raw_cell, raw_cell_start) in enumerate(cells):
            date_match = _LAYOUT_FULL_DATE.search(raw_cell)
            if date_match is None:
                continue

            raw_remainder = raw_cell[date_match.end() :]
            remainder = _compact_layout_value(raw_remainder)
            if remainder:
                if date_index + 1 >= len(cells):
                    continue
                origin = remainder
                remainder_leading = len(raw_remainder) - len(raw_remainder.lstrip())
                origin_start = raw_cell_start + date_match.end() + remainder_leading
                destination_index = date_index + 1
                destination = _compact_layout_value(cells[destination_index][0])
                destination_start = cells[destination_index][1]
            else:
                if date_index + 2 >= len(cells):
                    continue
                origin_index = date_index + 1
                destination_index = date_index + 2
                origin = _compact_layout_value(cells[origin_index][0])
                destination = _compact_layout_value(cells[destination_index][0])
                origin_start = cells[origin_index][1]
                destination_start = cells[destination_index][1]

            # Wrapped cells keep their horizontal position in PDF layout text.
            # Use that position to attach bare fragments such as ``大门`` or
            # ``限公司-东1门`` even when no parenthesis signals a continuation.
            anchors: list[tuple[int, str | None]] = [
                (origin_start, "origin"),
                (destination_start, "destination"),
                *((start, None) for _, start in cells[destination_index + 1 :]),
            ]
            for continuation_line in raw_lines[line_index + 1 : line_index + 4]:
                continuation_cells = [
                    (_compact_layout_value(value), start)
                    for value, start in _layout_cells(continuation_line)
                ]
                continuation_cells = [
                    (value, start)
                    for value, start in continuation_cells
                    if _valid_route_continuation(value)
                ]
                if not continuation_cells:
                    if not continuation_line.strip():
                        break
                    continue

                consumed: set[int] = set()
                for continuation_index, (value, _start) in enumerate(continuation_cells):
                    if _unclosed_parenthesis(origin):
                        origin += value
                        consumed.add(continuation_index)
                    elif _unclosed_parenthesis(destination):
                        destination += value
                        consumed.add(continuation_index)
                    else:
                        break

                for continuation_index, (value, start) in enumerate(continuation_cells):
                    if continuation_index in consumed:
                        continue
                    _anchor_start, target = min(anchors, key=lambda anchor: abs(anchor[0] - start))
                    if target == "origin":
                        origin += value
                    elif target == "destination":
                        destination += value

            if (
                _valid_route_location(origin)
                and _valid_route_location(destination)
                and origin != destination
            ):
                return _date_from_match(date_match, 2000), f"{origin}-{destination}"
    return None, None


def extract_passenger_route_from_layout(layout_text: str) -> str | None:
    return extract_passenger_fields_from_layout(layout_text)[1]


def extract_passenger_transport_from_layout(layout_text: str) -> str | None:
    """Read an explicit vehicle cell after the dated route, never company names."""
    if not all(header in layout_text for header in _PASSENGER_ROW_HEADERS):
        return None
    candidates: set[str] = set()
    for raw in layout_text.splitlines():
        cells = _layout_cells(raw)
        for index, (value, _position) in enumerate(cells):
            match = _LAYOUT_FULL_DATE.search(value)
            if match is None or _date_from_match(match, 2000) is None:
                continue
            # Some native PDFs merge the date and first location into a cell.
            merged_origin = bool(_compact_layout_value(value[match.end() :]))
            after_route = index + (2 if merged_origin else 3)
            for candidate, _position in cells[after_route:]:
                compact = _compact_layout_value(candidate)
                if compact in _PASSENGER_TRANSPORT_VALUES:
                    candidates.add(compact)
    return candidates.pop() if len(candidates) == 1 else None


def extract_passenger_occurrence_date_from_layout(layout_text: str) -> date | None:
    return extract_passenger_fields_from_layout(layout_text)[0]


def _passenger_route_from_flattened_lines(lines: list[OcrLine]) -> str | None:
    """Recover two location cells from OCR that flattened a passenger table row."""

    texts = [line.text.strip() for line in lines if line.text.strip()]
    if not all(any(header in text for text in texts) for header in _PASSENGER_ROW_HEADERS):
        return None

    for date_index, text in enumerate(texts):
        if "开票日期" in text or _FULL_DATE.search(text) is None:
            continue
        locations: list[str] = []
        continuations: list[str] = []
        saw_row_marker = False
        for raw_value in texts[date_index + 1 : date_index + 12]:
            value = _compact_layout_value(raw_value)
            if any(
                marker in value
                for marker in (
                    "价税合计",
                    "开票人",
                    "电子发票",
                    "发票号码",
                    "购买方",
                    "销售方",
                )
            ) or value in {
                "备",
                "注",
                "备注",
            }:
                break
            if value in _PASSENGER_ROW_NON_LOCATIONS:
                saw_row_marker = True
                continue
            if _ROUTE_PAREN_CONTINUATION.fullmatch(value):
                continuations.append(value)
            elif saw_row_marker and _valid_route_continuation(value):
                continuations.append(value)
            elif not saw_row_marker and _valid_route_location(value):
                locations.append(value)

        if len(locations) < 2 or not saw_row_marker:
            continue
        origin, destination = locations[:2]
        for continuation in continuations:
            if _unclosed_parenthesis(origin):
                origin += continuation
            elif _unclosed_parenthesis(destination):
                destination += continuation
            elif len(continuation) <= 12:
                # Once grade/transport cells have been observed, subsequent
                # short text belongs to a visually wrapped route row. Without
                # coordinates, the only closed-cell case we can safely repair
                # is the trailing destination fragment.
                destination += continuation
        if (
            not _unclosed_parenthesis(origin)
            and not _unclosed_parenthesis(destination)
            and _valid_route_location(origin)
            and _valid_route_location(destination)
            and origin != destination
        ):
            return f"{origin}-{destination}"
    return None


def extract_route(lines: list[OcrLine]) -> str | None:
    for line in lines:
        if line.text.startswith(PASSENGER_ROUTE_PREFIX):
            route = line.text.removeprefix(PASSENGER_ROUTE_PREFIX).strip()
            if route:
                return route[:200]
    passenger_route = _passenger_route_from_flattened_lines(lines)
    if passenger_route is not None:
        return passenger_route
    joined = " ".join(line.text for line in lines)
    match = _ROUTE.search(joined)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    station_lines = [
        line.text.strip()
        for line in lines
        if re.fullmatch(r"[\u4e00-\u9fff]{2,10}(?:站|南|北|东|西)", line.text.strip())
    ]
    if len(station_lines) >= 2:
        return f"{station_lines[0]}-{station_lines[1]}"
    return None


def extract_train_route(lines: list[OcrLine]) -> str | None:
    """Extract a rail route, retaining pinyin as a Linux font-fallback safety net."""

    route = extract_route(lines)
    if route is not None:
        return route

    texts = [line.text.strip() for line in lines if line.text.strip()]
    for index, text in enumerate(texts):
        if _TRAIN_NUMBER.fullmatch("".join(text.split())) is None:
            continue
        candidates: list[str] = []
        for value in texts[index + 1 : index + 5]:
            if not _PINYIN_STATION.fullmatch(value):
                continue
            compact = "".join(value.split()).lower()
            if not 4 <= len(compact) <= 40:
                continue
            candidates.append(compact[0].upper() + compact[1:])
            if len(candidates) == 2:
                if candidates[0] != candidates[1]:
                    return f"{candidates[0]}-{candidates[1]}"
                break
    return None

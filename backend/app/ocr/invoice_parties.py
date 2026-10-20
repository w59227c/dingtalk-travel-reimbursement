"""Recover the seller field without interpreting company names as business rules."""

from __future__ import annotations

import re
from contextlib import ExitStack

from app.ocr.types import OcrLine

SELLER_NAME_PREFIX = "销售方名称："
_EXPLICIT_NAME = re.compile(r"(?:销\s*售|销|开\s*票)\s*方\s*名\s*称\s*[:：]\s*")
_NAME_FIELD = re.compile(r"(?<!\S)名称\s*[:：]\s*(.*)$")
_PARTY_FIELD = re.compile(r"(?:销\s*售|购\s*买)\s*方(?:\s*信\s*息)?")
_FIELD_BOUNDARY = re.compile(
    r"名称|统一社会信用代码|纳税人|项目|价税合计|购买方|销售方|出发地|到达地"
)
_NAME_END = re.compile(
    r"\s{2,}|(?:销\s*售|购\s*买|销|开\s*票)\s*方(?:\s*信\s*息)?"
    r"\s*(?:名\s*称\s*[:：]|[:：])|(?:统一社会信用代码|纳税人识别号)\s*[:：]"
)
_NON_BUSINESS_FIELD = re.compile(r"购买方|购方|出发地|到达地|行程路线")
_BUSINESS_SECTION = re.compile(
    r"^(?:销售方|销方|开票方|项目名称|商品名称|服务名称|价税合计|合计|开票日期|发票号码)"
    r"|^[*＊][^*＊]{1,40}[*＊]"
)


def _name_value(value: str) -> str | None:
    value = _NAME_END.split(value.strip(), maxsplit=1)[0].strip()
    if 2 <= len(value) <= 200 and not _FIELD_BOUNDARY.match(value):
        return value
    return None


def invoice_keyword_lines(lines: list[OcrLine]) -> list[OcrLine]:
    """Exclude labelled buyer/route fields and their continuation rows from keywords."""
    visible: list[OcrLine] = []
    excluded = False
    for index, line in enumerate(lines):
        compact = "".join(line.text.split())
        vertical_buyer = (
            compact.startswith("购")
            and index + 2 < len(lines)
            and lines[index + 1].text.lstrip().startswith("买")
            and lines[index + 2].text.lstrip().startswith("方")
        )
        if _NON_BUSINESS_FIELD.search(compact) or vertical_buyer:
            excluded = True
            continue
        if excluded and _BUSINESS_SECTION.search(compact):
            excluded = False
        if not excluded:
            visible.append(line)
    return visible


def invoice_seller_name(layout_text: str) -> str | None:
    """Use explicit labels or party columns; ambiguous or unlabelled names stay empty."""
    lines = layout_text.splitlines()
    candidates: set[str] = set()
    anchors: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        for match in _EXPLICIT_NAME.finditer(line):
            value = line[match.end() :]
            if not value and index + 1 < len(lines):
                value = lines[index + 1].strip()
            if name := _name_value(value):
                candidates.add(name)
        for match in _PARTY_FIELD.finditer(line):
            role = "seller" if match.group().startswith("销") else "buyer"
            anchors.append((index, match.start(), role))
        # Electronic invoice templates often print the party label vertically.
        for column, value in enumerate(line):
            if value not in {"销", "购"} or index + 2 >= len(lines):
                continue
            middle = "售" if value == "销" else "买"
            if (
                lines[index + 1][column : column + 1] == middle
                and lines[index + 2][column : column + 1] == "方"
            ):
                anchors.append((index, column, "seller" if value == "销" else "buyer"))
    for row, column, role in anchors:
        if role != "seller":
            continue
        right = min(
            (x for y, x, _ in anchors if abs(y - row) <= 2 and x > column),
            default=max((len(line) for line in lines), default=0),
        )
        paired_columns = any(
            other_role == "buyer" and abs(y - row) <= 2 and abs(x - column) > 2
            for y, x, other_role in anchors
        )
        start = max(0, row - 2) if paired_columns else row
        end = min(
            (y for y, x, _ in anchors if y > row and abs(x - column) <= 2),
            default=len(lines),
        )
        for line in lines[start : min(end, row + 3)]:
            section = line[column:right]
            if match := _NAME_FIELD.search(section):
                if name := _name_value(match.group(1)):
                    candidates.add(name)
    return next(iter(candidates)) if len(candidates) == 1 else None


_Character = tuple[str, float, float, float, float]


def _positioned_seller_name(characters: list[_Character]) -> str | None:
    """Read the name beside a vertical seller label, independently of stream order."""
    candidates: set[str] = set()
    starts = [item for item in characters if item[0] == "销"]
    if len(starts) > 8:
        return None
    for start in starts:
        _, left, bottom, right, top = start
        height = top - bottom
        if height <= 0:
            continue
        vertical = [
            item
            for item in characters
            if abs(item[1] - left) < height / 2
            and bottom - height * 3 < item[2] <= bottom + 0.1
        ]
        if "".join(item[0] for item in sorted(vertical, key=lambda item: -item[2])) != "销售方":
            continue
        # The name is printed beside the upper part of the party label. Crop at
        # another party column instead of including its buyer name on the right.
        boundary = min(
            (
                item[1]
                for item in characters
                if item[0] in {"购", "销"}
                and item[1] > right
                and abs(item[2] - bottom) < height
            ),
            default=float("inf"),
        )
        names = [
            item
            for item in characters
            if item[0] == "名"
            and right < item[1] < min(boundary, right + height * 4)
            and -height * 1.5 < item[2] - bottom <= height / 2
        ]
        if len(names) > 4:
            return None
        for name in names:
            center = (name[2] + name[4]) / 2
            row = sorted(
                (
                    item
                    for item in characters
                    if name[1] <= item[1] < boundary
                    and abs((item[2] + item[4]) / 2 - center) < height / 2
                ),
                key=lambda item: item[1],
            )
            # Reject gaps/overlapping text rather than join unrelated fields.
            continuous: list[_Character] = []
            for item in row:
                if continuous and (
                    item[1] - continuous[-1][3] > height * 2
                    or item[1] < continuous[-1][3] - height / 3
                ):
                    break
                continuous.append(item)
            text = "".join(item[0] for item in continuous)
            if match := re.fullmatch(r"名称[:：](.+)", text):
                if value := _name_value(match.group(1)):
                    candidates.add(value)
    return next(iter(candidates)) if len(candidates) == 1 else None


def invoice_seller_from_pdf(path: str, *, max_characters: int = 100_000) -> str | None:
    """Supplement native text only inside an already validated, bounded PDF worker."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None
    try:
        with ExitStack() as resources:
            document = pdfium.PdfDocument(path)
            resources.callback(document.close)
            page = document[0]
            resources.callback(page.close)
            if page.get_rotation():
                return None
            textpage = page.get_textpage()
            resources.callback(textpage.close)
            count = textpage.count_chars()
            if count > max_characters:
                return None
            characters = []
            for index in range(count):
                character = textpage.get_text_range(index, 1)
                if character.strip():
                    characters.append((character, *textpage.get_charbox(index)))
            return _positioned_seller_name(characters)
    except (pdfium.PdfiumError, OSError, ValueError, IndexError):
        return None

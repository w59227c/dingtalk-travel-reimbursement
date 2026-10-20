import subprocess
import sys

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
)

from app.domain.categories import ExpenseCategory
from app.ocr.engine import FakeOcrEngine
from app.ocr.invoice_parties import (
    _positioned_seller_name,
    invoice_seller_from_pdf,
    invoice_seller_name,
)
from app.ocr.itinerary_worker import recognize_itinerary_worker
from app.ocr.parsers import ReceiptParserRegistry
from app.ocr.types import OcrLine, ParseContext, ReceiptKeywordRule
from app.ocr.workers import _append_passenger_fields


def _positioned_party_pdf(path, *, seller_left=False, buyer_only=False, conflicting=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=640, height=842)
    cells = []

    def cell(x, y, text):
        cells.append((x, y, text))

    buyer_x, seller_x = (300, 20) if seller_left else (20, 300)
    # Values deliberately precede every label in PDF stream order.
    cell(buyer_x + 50, 700, "示例航空买方有限公司")
    if not buyer_only:
        cell(seller_x + 50, 700, "示例出行销售公司")
    for x, role in [(buyer_x, "购买方"), (seller_x, "购买方" if buyer_only else "销售方")]:
        for index, character in enumerate(role):
            cell(x, 710 - index * 13, character)
        cell(x + 16, 700, "名称：")
    if conflicting:
        for index, character in enumerate("销售方"):
            cell(seller_x, 510 - index * 13, character)
        cell(seller_x + 16, 500, "名称：另一家销售公司")
    cell(20, 650, "电子发票（普通发票）")
    cell(20, 620, "开票日期：2026年07月01日")
    cell(20, 600, "价税合计（小写）￥100.00")
    cell(20, 580, "*交通运输服务*客运服务费")
    cell(20, 560, "出行日期：2026年07月01日")
    cell(20, 540, "甲地-乙地")
    # Embed simple vector glyphs with explicit Unicode mappings. Unembedded
    # standard CJK fonts still depend on the host's available substitute fonts.
    codes = {character: index for index, character in enumerate(
        sorted({character for _, _, text in cells for character in text}), start=1,
    )}
    assert len(codes) < 100
    glyph = DecodedStreamObject()
    glyph.set_data(b"1000 0 0 0 1000 1000 d1 0 0 1000 1000 re f")
    glyph_ref = writer._add_object(glyph)
    unicode_map = DecodedStreamObject()
    mappings = "\n".join(
        f"<{code:02x}> <{character.encode('utf-16be').hex()}>"
        for character, code in codes.items()
    )
    unicode_map.set_data((
        "/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /SyntheticUnicode def /CMapType 2 def\n"
        "1 begincodespacerange <00> <FF> endcodespacerange\n"
        f"{len(codes)} beginbfchar\n{mappings}\nendbfchar\n"
        "endcmap CMapName currentdict /CMap defineresource pop end end"
    ).encode("ascii"))
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type3"),
        NameObject("/FontBBox"): ArrayObject([NumberObject(v) for v in (0, 0, 1000, 1000)]),
        NameObject("/FontMatrix"): ArrayObject([
            FloatObject(v) for v in (0.001, 0, 0, 0.001, 0, 0)
        ]),
        NameObject("/CharProcs"): DictionaryObject({
            NameObject(f"/g{code}"): glyph_ref for code in codes.values()
        }),
        NameObject("/Encoding"): DictionaryObject({
            NameObject("/Type"): NameObject("/Encoding"),
            NameObject("/Differences"): ArrayObject([
                NumberObject(1), *(NameObject(f"/g{code}") for code in codes.values()),
            ]),
        }),
        NameObject("/FirstChar"): NumberObject(1),
        NameObject("/LastChar"): NumberObject(len(codes)),
        NameObject("/Widths"): ArrayObject([NumberObject(1000) for _ in codes]),
        NameObject("/Resources"): DictionaryObject(),
        NameObject("/ToUnicode"): writer._add_object(unicode_map),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
    })
    commands = []
    for x, y, text in cells:
        encoded = "".join(f"{codes[character]:02x}" for character in text)
        commands.append(f"BT /F1 10 Tf 1 0 0 1 {x} {y} Tm <{encoded}> Tj ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(commands).encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(path)
    return path


@pytest.mark.parametrize("seller_left", [False, True])
def test_pdf_seller_uses_coordinates_independently_of_stream_order(tmp_path, seller_left):
    path = _positioned_party_pdf(tmp_path / "seller.pdf", seller_left=seller_left)
    assert invoice_seller_from_pdf(str(path)) == "示例出行销售公司"
    assert invoice_seller_from_pdf(str(path), max_characters=10) is None


@pytest.mark.parametrize("seller_left", [False, True])
def test_pdf_seller_fixture_does_not_require_system_fonts(tmp_path, seller_left):
    path = _positioned_party_pdf(tmp_path / "seller.pdf", seller_left=seller_left)
    # PDFium's font hooks are process-global; isolate this environment in a child.
    script = """
import ctypes
import sys
import pypdfium2 as pdfium
from app.ocr.invoice_parties import invoice_seller_from_pdf

font_info = pdfium.raw.FPDF_SYSFONTINFO()
font_info.version = 1
callbacks = []
for name, callback_type in font_info._fields_:
    if name != "version":
        callback = callback_type(lambda *args: 0)
        callbacks.append(callback)
        setattr(font_info, name, callback)
pdfium.raw.FPDF_SetSystemFontInfo(ctypes.byref(font_info))
assert invoice_seller_from_pdf(sys.argv[1]) == "示例出行销售公司"
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("changes", [{"buyer_only": True}, {"conflicting": True}])
def test_pdf_seller_rejects_missing_or_conflicting_party_labels(tmp_path, changes):
    path = _positioned_party_pdf(tmp_path / "seller.pdf", **changes)
    assert invoice_seller_from_pdf(str(path)) is None


def test_material_worker_recovers_native_seller_without_scanning(settings_factory, tmp_path):
    class NoScan(FakeOcrEngine):
        def recognize(self, path):
            raise AssertionError("Native seller coordinates should not need OCR")

    path = _positioned_party_pdf(tmp_path / "seller.pdf")
    settings = settings_factory()
    rules = (
        ReceiptKeywordRule("示例出行", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
        ReceiptKeywordRule("客运服务费", ExpenseCategory.LOCAL_TRANSPORT),
        ReceiptKeywordRule("航空", ExpenseCategory.AIRFARE),
    )
    result = recognize_itinerary_worker(
        str(path), "pdf", {}, settings.pdf_limits, settings.ocr_worker_limits, 2026,
        NoScan(), classify=True, keyword_rules=rules,
    )
    assert result["ok"]
    assert result["materialKind"] == "expense"
    assert result["expense"].category == ExpenseCategory.LOCAL_TRANSPORT
    assert result["expense"].transport_type == "ride_hailing"
    assert result["expense"].requires_itinerary


def test_positioned_seller_does_not_read_name_above_its_party_block():
    characters = [
        (c, 20, y, 30, y + 10) for c, y in zip("销售方", [100, 90, 80], strict=True)
    ]
    characters.extend(
        (c, 20, y, 30, y + 10) for c, y in zip("购买方", [140, 130, 120], strict=True)
    )
    characters.extend(
        (c, 36 + index * 10, 110, 46 + index * 10, 120)
        for index, c in enumerate("名称：示例航空买方公司")
    )
    assert _positioned_seller_name(characters) is None


@pytest.mark.parametrize("prefix", ["项目名称：", "商品名称：", "服务名称："])
@pytest.mark.parametrize("category", [ExpenseCategory.AIRFARE, ExpenseCategory.LOCAL_TRANSPORT])
@pytest.mark.parametrize("tax_label", ["*交通运输服务*", ""])
def test_service_name_fields_remain_business_evidence(prefix, category, tax_label):
    local = category == ExpenseCategory.LOCAL_TRANSPORT
    parsed = ReceiptParserRegistry(
        keyword_rules=(
            ReceiptKeywordRule("示例出行", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
            ReceiptKeywordRule("航空", ExpenseCategory.AIRFARE),
        )
    ).parse(
        [
            OcrLine(text, 1)
            for text in (
                "电子发票 旅客运输服务",
                "销售方名称：示例出行公司" if local else "销售方名称：示例科技公司",
                prefix + tax_label + ("客运服务费" if local else "国内航空旅客运输服务"),
                "价税合计 ￥100.00",
            )
        ],
        ParseContext(2026),
    )
    assert parsed.category == category
    if local:
        assert parsed.transport_type == "ride_hailing"
        assert parsed.requires_itinerary


@pytest.mark.parametrize(
    "layout,expected",
    [
        ("销售方名称：示例出行公司", "示例出行公司"),
        ("销售方名称：\n示例出行公司", "示例出行公司"),
        ("销售方信息\n名称：示例出行公司\n纳税人识别号：123", "示例出行公司"),
        ("销售方名称：\n纳税人识别号：123", None),
        ("购买方名称：航空科技有限公司\n示例出行公司", None),
        ("销售方名称：示例出行公司\n销售方名称：另一家公司", None),
        ("出发地：某地\n到达地：航空大酒店有限公司", None),
        ("购买方信息\n名称：航空科技有限公司\n销售方信息\n名称：示例出行公司", "示例出行公司"),
        ("购买方信息\n名称：航空科技有限公司\n销售方信息\n纳税人识别号：123", None),
        ("销售方信息\n纳税人识别号：123\n购买方信息 名称：航空科技有限公司", None),
        ("销售方名称：示例出行公司 购买方名称：航空科技有限公司", "示例出行公司"),
        ("销售方名称：示例出行公司购买方名称：航空科技有限公司", "示例出行公司"),
        ("销售方名称：购买方名称：航空科技有限公司", None),
        ("购买方名称：示例买方公司销售方名称：示例出行公司", "示例出行公司"),
        ("购买方名称：示例买方公司 销售方名称：示例出行公司", "示例出行公司"),
        ("购买方名称：示例买方公司  销售方名称：示例出行公司", "示例出行公司"),
        ("销售方名称：示例出行公司销售方名称：另一家公司", None),
    ],
)
def test_seller_requires_unambiguous_party_fields(layout, expected):
    assert invoice_seller_name(layout) == expected


def _party_layout(seller="示例出行公司"):
    buyer = "购  名称：航空科技有限公司"
    seller_column = len(buyer) + 20
    return "\n".join(
        [
            f"{buyer}{' ' * 20}销  名称：{seller}",
            "买" + " " * (seller_column - 1) + "售",
            "方" + " " * (seller_column - 1) + "方",
        ]
    )


@pytest.mark.parametrize(
    "category,transport_type",
    [
        (ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
        (ExpenseCategory.LOCAL_TRANSPORT, "taxi"),
        (ExpenseCategory.LOCAL_TRANSPORT, None),
        (ExpenseCategory.OFFICE, None),
    ],
)
def test_layout_seller_matching_is_independent_of_keyword_group(category, transport_type):
    layout = _party_layout()
    assert invoice_seller_name(layout) == "示例出行公司"
    raw = [
        ("电子发票 旅客运输服务", 1),
        ("购买方名称：航空科技有限公司", 1),
        ("到达地：航空大酒店有限公司", 1),
        ("价税合计 ￥20.00", 1),
    ]
    _append_passenger_fields(raw, "\n".join(text for text, _ in raw), layout)
    rules = (
        ReceiptKeywordRule("示例出行", category, transport_type),
        ReceiptKeywordRule("航空", ExpenseCategory.AIRFARE),
    )
    parsed = ReceiptParserRegistry(keyword_rules=rules).parse(
        [OcrLine(text, confidence) for text, confidence in raw], ParseContext(2026)
    )
    assert parsed.category is category
    assert parsed.transport_type == transport_type
    assert parsed.requires_itinerary is (transport_type == "ride_hailing")


def test_operator_in_buyer_or_destination_does_not_override_identified_seller():
    document = [
        OcrLine(text, 1)
        for text in (
            "电子发票 旅客运输服务",
            "销售方名称：其他科技公司",
            "购买方名称：示例出行公司",
            "到达地：示例出行公司",
            "价税合计 ￥20.00",
        )
    ]
    parsed = ReceiptParserRegistry(
        keyword_rules=(
            ReceiptKeywordRule("示例出行", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
        )
    ).parse(document, ParseContext(2026))
    assert parsed.category is ExpenseCategory.OTHER and not parsed.requires_itinerary


def test_identified_operator_seller_still_requires_transport_service_evidence():
    parsed = ReceiptParserRegistry(
        keyword_rules=(
            ReceiptKeywordRule("示例出行", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
        )
    ).parse(
        [OcrLine(text, 1) for text in (
            "电子发票", "销售方名称：示例出行公司", "广告推广服务", "价税合计 ￥20.00",
        )],
        ParseContext(2026),
    )
    assert parsed.category is ExpenseCategory.OTHER and not parsed.requires_itinerary


@pytest.mark.parametrize(
    "fields",
    [
        ("购买方名称：示例出行公司",),
        ("购买方信息", "名称：示例出行公司", "纳税人识别号：123"),
        ("出发地：示例出行公司",),
        ("到达地：示例出行公司",),
        ("到达地：", "示例出行公司"),
        ("行程路线：某地—示例出行公司",),
        ("出行日期 出发地 到达地 交通工具类型", "2026-09-01 某地 示例出行公司 出租汽车"),
    ],
)
def test_missing_seller_does_not_match_operators_in_buyer_or_route_fields(fields):
    document = [
        OcrLine(text, 1)
        for text in ("电子发票 旅客运输服务", *fields, "价税合计 ￥20.00")
    ]
    parsed = ReceiptParserRegistry(keyword_rules=(
        ReceiptKeywordRule("示例出行", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
    )).parse(document, ParseContext(2026))
    assert parsed.category is ExpenseCategory.OTHER
    assert parsed.transport_type is None and not parsed.requires_itinerary


@pytest.mark.parametrize("separator", ["", " ", "  "])
def test_merged_party_fields_keep_buyer_keywords_out_of_seller_classification(separator):
    document = [OcrLine(text, 1) for text in (
        "电子发票 旅客运输服务",
        f"销售方名称：示例出租车公司{separator}购买方名称：示例航空科技公司",
        "交通工具类型：出租汽车", "价税合计 ￥20.00",
    )]
    parsed = ReceiptParserRegistry().parse(document, ParseContext(2026))
    assert parsed.category is ExpenseCategory.LOCAL_TRANSPORT
    assert parsed.transport_type == "taxi" and not parsed.requires_itinerary


@pytest.mark.parametrize("separator", ["", " ", "  "])
def test_seller_after_buyer_on_same_line_preserves_ride_hailing_requirement(separator):
    parsed = ReceiptParserRegistry().parse([OcrLine(text, 1) for text in (
        "电子发票 旅客运输服务",
        f"购买方名称：示例买方公司{separator}销售方名称：示例滴滴出行公司",
        "*交通运输服务*客运服务费", "价税合计 ￥20.00",
    )], ParseContext(2026))
    assert parsed.category is ExpenseCategory.LOCAL_TRANSPORT
    assert parsed.transport_type == "ride_hailing" and parsed.requires_itinerary


def test_buyer_name_does_not_supply_transport_service_context_for_a_seller():
    parsed = ReceiptParserRegistry(keyword_rules=(
        ReceiptKeywordRule("示例出行", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
    )).parse([OcrLine(text, 1) for text in (
        "电子发票", "购买方名称：测试交通运输服务有限公司",
        "销售方名称：示例出行公司", "广告推广服务", "价税合计 ￥20.00",
    )], ParseContext(2026))
    assert parsed.category is ExpenseCategory.OTHER and not parsed.requires_itinerary


@pytest.mark.parametrize(
    "row,expected",
    [
        (("2026-09-01 项目地 机场",), ExpenseCategory.OTHER),
        (("2026-09-01 项目地 合肥 机场",), ExpenseCategory.OTHER),
        (("2026-09-01", "项目地", "机场"), ExpenseCategory.OTHER),
        (("2026-09-01 项目地 机场 出租汽车",), ExpenseCategory.LOCAL_TRANSPORT),
        (("2026-09-01", "项目地", "机场", "出租汽车"), ExpenseCategory.LOCAL_TRANSPORT),
    ],
)
def test_transport_table_does_not_use_destination_as_a_missing_vehicle(row, expected):
    parsed = ReceiptParserRegistry().parse([OcrLine(text, 1) for text in (
        "电子发票 旅客运输服务", "出行日期 出发地 到达地 交通工具类型",
        *row, "价税合计 ￥20.00",
    )], ParseContext(2026))
    assert parsed.category is expected
    assert parsed.transport_type == (
        "taxi" if expected is ExpenseCategory.LOCAL_TRANSPORT else None
    )


@pytest.mark.parametrize("name", ["示例出行交通运输服务有限公司", "示例出行旅客运输服务中心"])
def test_seller_name_does_not_substitute_for_transport_service_evidence(name):
    parsed = ReceiptParserRegistry(keyword_rules=(
        ReceiptKeywordRule("示例出行", ExpenseCategory.LOCAL_TRANSPORT, "ride_hailing"),
    )).parse([OcrLine(text, 1) for text in (
        "电子发票", f"销售方名称：{name}", "广告推广服务", "价税合计 ￥20.00",
    )], ParseContext(2026))
    assert parsed.category is ExpenseCategory.OTHER and not parsed.requires_itinerary

from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.ocr.engine import FakeOcrEngine
from app.ocr.itinerary import ItineraryPage, parse_itinerary_pages
from app.ocr.itinerary_pdf import positioned_itinerary_layout
from app.ocr.types import OcrLine


def _table_pdf(path: Path, *, broken_amount=False, rows=1, missing_header=False, overlap=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=640, height=842)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Courier"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        }
    )
    commands = []

    def cell(x, y, text):
        commands.append(f"BT /F1 8 Tf 1 0 0 1 {x} {y} Tm ({text}) Tj ET")

    cell(40, 800, "Ride itinerary CNY")
    cell(40, 780, f"Total amount: {19.98 * rows:.2f}")
    mileage_x = 300 + len("DestinationCompanyBuildingNumber") * 4.8 + 0.7
    if overlap:
        mileage_x -= 4
    for x, text in [
        (40, "No."),
        (80, "Date"),
        (210, "Origin"),
        (300, "Destination"),
        (mileage_x, "Mileage"),
        (510, "Amount"),
    ]:
        if not missing_header or text != "Destination":
            cell(x, 740, text)
    for index in range(rows):
        y = 700 - index * 40
        cell(40, y - 4, str(index + 1))
        cell(80, y, f"2026-07-{index + 2:02d}")
        cell(80, y - 8, "08:41")
        cell(210, y - 4, f"Origin{index + 1}")
        # The first address line ends only half a point before the mileage column.
        cell(300, y, "DestinationCompanyBuildingNumber")
        cell(300, y - 8, "EastGate")
        cell(mileage_x, y - 4, "7.4")
        cell(510, y - 4, "unreadable" if broken_amount else "19.98")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(commands).encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as output:
        writer.write(output)
    return path


@pytest.mark.parametrize("rows", [1, 2])
def test_pdf_coordinates_separate_mileage_and_join_wrapped_routes(tmp_path, rows):
    path = _table_pdf(tmp_path / "table.pdf", rows=rows)
    layout = positioned_itinerary_layout(str(path), 0, max_characters=10000)
    assert layout
    text = f"Itinerary CNY\nTotal amount: {19.98 * rows:.2f}"
    result = parse_itinerary_pages(
        [
            ItineraryPage(
                1, tuple(OcrLine(line, 1) for line in text.splitlines()), layout, "pdf_text"
            )
        ],
        page_count=1,
        reference_year=2026,
    )
    assert result.complete
    assert len(result.trips) == rows
    assert result.trips[0].origin == "Origin1"
    assert result.trips[0].destination == "DestinationCompanyBuildingNumberEastGate"
    assert str(result.trips[0].amount) == "19.98"


@pytest.mark.parametrize(
    "changes", [{"broken_amount": True}, {"missing_header": True}, {"overlap": True}]
)
def test_ambiguous_positioned_tables_are_not_used(tmp_path, changes):
    path = _table_pdf(tmp_path / "table.pdf", **changes)
    assert positioned_itinerary_layout(str(path), 0, max_characters=10000) is None


def test_positioned_extraction_respects_character_budget(tmp_path):
    path = _table_pdf(tmp_path / "table.pdf")
    assert positioned_itinerary_layout(str(path), 0, max_characters=10) is None


@pytest.mark.parametrize("classify", [False, True])
def test_worker_uses_positioned_native_table_before_loading_ocr(
    settings_factory, tmp_path, classify, run_itinerary_in_process
):
    class NoScan(FakeOcrEngine):
        def recognize(self, path):
            raise AssertionError("Native table should not need OCR")

    path = _table_pdf(tmp_path / "table.pdf")
    settings = settings_factory()
    result = run_itinerary_in_process(
        str(path),
        "pdf",
        {},
        settings.pdf_limits,
        settings.ocr_worker_limits,
        2026,
        NoScan(),
        classify=classify,
    )
    assert result["parsed"].complete
    assert result["parsed"].source == "pdf_text"
    assert result["parsed"].trips[0].destination == "DestinationCompanyBuildingNumberEastGate"

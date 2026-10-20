"""Conservative material routing; ambiguous content never becomes an expense."""

from __future__ import annotations

import re
from collections.abc import Sequence

from app.ocr.itinerary import ItineraryPage
from app.ocr.payment_proofs import classify_bank_payment
from app.ocr.special_receipts import PhysicalTaxiReceiptParser


def classify_material(pages: Sequence[ItineraryPage], *, page_count: int) -> tuple[str, str | None]:
    text = "\n".join(line.text for page in pages for line in page.lines)
    compact = re.sub(r"\s+", "", text)
    lower = text.lower()
    airline = bool(re.search(r"航空运输电子客票行程单|航空运输电子客票|航空运输客票", compact))
    invoice = (
        bool(
            re.search(
                r"电子发票|增值税.*发票|通用机打发票|出租车.*发票|价税合计|铁路电子客票|铁路.*客票",
                compact,
            )
        )
        or airline
        # Stamps often hide 机打/出租车 in a paper receipt's title. Require both
        # invoice evidence and the existing meter-field signature, not 发票 alone.
        or (
            "发票" in compact
            and PhysicalTaxiReceiptParser().match([line for page in pages for line in page.lines])
            >= 0.9
        )
    )
    payment_title = bool(
        re.search(
            r"payment\s+(?:receipt|confirmation)|transaction\s+(?:receipt|details)|"
            r"(?:bank\s+)?transfer\s+(?:receipt|confirmation)",
            lower,
        )
    )
    foreign_invoice = (
        not payment_title
        and bool(re.search(r"\b(?:guest\s+invoice|tax\s+invoice|invoice|receipt)\b", lower))
        and bool(re.search(r"\b(?:total|amount|paid|subtotal)\b", lower))
    )
    # A generic 明细 label is also found on invoices and payment screens. Require
    # a lodging bill title and multiple independent stay/table field signatures.
    hotel_bill = (
        bool(
            re.search(
                r"住宿明细|账单明细|酒店账单|宾客账单|水单|hotel\s+bill|guest\s+folio|酒店|宾馆|hotel",
                lower,
            )
        )
        and bool(re.search(r"住客|宾客|客人|入住人|姓名|guest", lower))
        and bool(re.search(r"入住|抵店|到店|check[ -]?in|arrival", lower))
        and bool(re.search(r"退房|离店|check[ -]?out|departure", lower))
        and bool(re.search(r"房价|房费|单价|rate|room\s+charge", lower))
    )
    # The heading and transport/table structure must agree. A mention in an
    # invoice's note, or an airline ticket heading, is not a taxi itinerary.
    itinerary_title = bool(
        re.search(r"行程单|行程明细|出行记录|itinerary|trip\s+details|ride\s+history", lower)
    )
    route_table = bool(re.search(r"起点|上车|出发地|起始地|origin|pickup", lower)) and bool(
        re.search(r"终点|下车|到达地|目的地|destination|dropoff", lower)
    )
    ride_signal = bool(
        re.search(r"滴滴|高德|网约车|出租车|打车|快车|专车|出行|用车|ride|trip", lower)
    )
    itinerary = itinerary_title and route_table and ride_signal and not airline
    bank_payment = classify_bank_payment(pages)
    payment = (
        payment_title
        or bool(
            re.search(
                r"支付成功|付款成功|交易成功|转账成功|付款凭证|银行回单|电子回单|payment\s+successful|paid\s+successfully",
                lower,
            )
        )
    ) and bool(
        re.search(
            r"付款方|收款方|收款人|商户|交易单号|支付金额|付款金额|实付|交易金额|收款账户|payee|amount",
            lower,
        )
    )
    if bank_payment is not None:
        payment = bank_payment == "posted"
    if invoice or foreign_invoice:
        if page_count != 1:
            return "unknown", "多页或混合票据请拆分为单张发票，或确认作为证明材料"
        if itinerary or payment:
            return "unknown", "材料同时包含发票和证明信息，请确认用途"
        return "expense", None
    if hotel_bill:
        # A bill can contain an amount due; it is never proof of successful payment.
        return "hotel_bill", None
    if itinerary and payment:
        return "unknown", "材料同时包含行程和付款信息，请确认用途"
    if itinerary:
        return "itinerary", None
    if payment:
        return "payment_proof", None
    # Legacy railway paper tickets do not necessarily contain the word 发票.
    if (
        page_count == 1
        and re.search(r"铁路|火车票|高铁|二等座|一等座", compact)
        and re.search(r"票价|车次|[GDCZTK]\d{1,5}", compact)
    ):
        return "expense", None
    return "unknown", "未能确定材料用途，请选择发票、行程单、住宿明细、付款凭证或其他材料"

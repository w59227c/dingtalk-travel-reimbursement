from __future__ import annotations

import re
from dataclasses import replace

from app.domain.categories import CATEGORY_BY_ID, ExpenseCategory
from app.ocr.document_evidence import extract_document_numbers, extract_rail_type
from app.ocr.extractors import (
    PASSENGER_OCCURRENCE_DATE_PREFIX,
    average_confidence,
    extract_amount,
    extract_date,
    extract_invoice_amount,
    extract_passenger_occurrence_date,
    extract_passenger_service_keywords,
    extract_passenger_transport_type,
    extract_route,
    extract_train_route,
    extract_train_travel_date,
    uses_invoice_date_as_occurrence,
)
from app.ocr.invoice_parties import invoice_keyword_lines, invoice_seller_name
from app.ocr.keyword_defaults import DEFAULT_RECEIPT_KEYWORD_RULES
from app.ocr.special_receipts import ForeignReceiptParser, PhysicalTaxiReceiptParser
from app.ocr.types import (
    BaseReceiptParser,
    OcrLine,
    ParseContext,
    ParsedExpense,
    ReceiptKeywordRule,
)

MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
MISSING_AMOUNT = "MISSING_AMOUNT"
MISSING_DATE = "MISSING_DATE"
MISSING_ROUTE = "MISSING_ROUTE"
LOW_OCR_CONFIDENCE = "LOW_OCR_CONFIDENCE"
QR_ISSUE_DATE_USED = "QR_ISSUE_DATE_USED"
INVOICE_DATE_USED_AS_OCCURRENCE = "INVOICE_DATE_USED_AS_OCCURRENCE"
_PASSENGER_TRANSPORT_KEYWORDS = (
    "旅客运输服务",
    "交通运输服务",
    "客运服务费",
    "交通工具类型",
)


def _with_transport_evidence(
    parsed: ParsedExpense,
    lines: list[OcrLine],
    keyword_rules: tuple[ReceiptKeywordRule, ...],
) -> ParsedExpense:
    if parsed.transport_type is not None:
        return parsed
    if parsed.category is ExpenseCategory.LOCAL_TRANSPORT:
        matches = GenericInvoiceParser(keyword_rules=keyword_rules).matching_rules(lines)
        types = {rule.transport_type for rule in matches if rule.transport_type is not None}
        # A ride-hailing provider can issue a ticket whose vehicle field says taxi.
        transport_type = (
            "ride_hailing" if "ride_hailing" in types else "taxi" if "taxi" in types else None
        )
        return replace(
            parsed,
            transport_type=transport_type,
            requires_itinerary=transport_type == "ride_hailing",
        )
    if parsed.category is ExpenseCategory.RAIL_FARE:
        return replace(parsed, transport_type="rail")
    if parsed.category is ExpenseCategory.LODGING:
        return replace(parsed, transport_type="hotel")
    return parsed


def is_passenger_transport_text(text: str) -> bool:
    compact = "".join(text.split())
    return any(keyword in compact for keyword in _PASSENGER_TRANSPORT_KEYWORDS)


def _warnings(lines: list[OcrLine], *, amount: object, parsed_date: object) -> list[str]:
    warnings: list[str] = []
    if amount is None:
        warnings.append(MISSING_AMOUNT)
    if parsed_date is None:
        warnings.append(MISSING_DATE)
    if average_confidence(lines) < 0.65:
        warnings.append(LOW_OCR_CONFIDENCE)
    return warnings


class TrainTicketParser:
    _keywords = ("铁路电子客票", "火车票", "车次", "二等座", "一等座", "检票口")

    def match(self, lines: list[OcrLine]) -> float:
        text = " ".join(line.text for line in lines)
        hits = sum(keyword in text for keyword in self._keywords)
        has_train_number = any(
            token and token[0] in "GDCZTK" and token[1:].isdigit()
            for token in text.replace("/", " ").split()
        )
        has_route_signal = "-" in text or "—" in text or "→" in text
        return min(
            1.0,
            hits * 0.25 + (0.2 if has_train_number else 0.0) + (0.15 if has_route_signal else 0.0),
        )

    def parse(self, lines: list[OcrLine], context: ParseContext) -> ParsedExpense:
        parsed_date = extract_train_travel_date(lines, context.reference_year)
        amount = extract_amount(lines, ("票价", "金额", "价税合计"))
        route = extract_train_route(lines)
        warnings = _warnings(lines, amount=amount, parsed_date=parsed_date)
        if uses_invoice_date_as_occurrence(
            lines,
            context.reference_year,
            parsed_date,
            ("出行日期", "乘车日期", "开车时间"),
        ):
            warnings.append(INVOICE_DATE_USED_AS_OCCURRENCE)
        if route is None:
            warnings.append(MISSING_ROUTE)
        parser_score = self.match(lines)
        confidence = min(average_confidence(lines), parser_score) if lines else 0.0
        return ParsedExpense(
            receipt_type="train",
            category=ExpenseCategory.RAIL_FARE,
            date=parsed_date,
            description=route,
            amount=amount,
            confidence=confidence,
            warnings=tuple(warnings),
        )


class GenericInvoiceParser:
    _invoice_keywords = ("发票", "价税合计", "开票日期", "发票号码", "旅客运输服务")

    def __init__(
        self,
        keyword_rules: tuple[ReceiptKeywordRule, ...] | None = None,
    ) -> None:
        self._keyword_rules = (
            DEFAULT_RECEIPT_KEYWORD_RULES if keyword_rules is None else keyword_rules
        )

    def match(self, lines: list[OcrLine]) -> float:
        text = " ".join(line.text for line in lines)
        hits = sum(keyword in text for keyword in self._invoice_keywords)
        return min(0.9, 0.2 + hits * 0.15) if hits else 0.0

    def _classification(
        self,
        lines: list[OcrLine],
    ) -> tuple[ExpenseCategory, bool]:
        matches = {rule.category for rule in self.matching_rules(lines)}
        if len(matches) == 1:
            return next(iter(matches)), False
        return ExpenseCategory.OTHER, True

    def matching_rules(self, lines: list[OcrLine]) -> tuple[ReceiptKeywordRule, ...]:
        keyword_lines = invoice_keyword_lines(lines)
        keyword_text = " ".join(line.text for line in keyword_lines).casefold()
        compact = "".join(keyword_text.split())
        seller = invoice_seller_name("\n".join(line.text for line in lines))
        seller_text = "".join(seller.casefold().split()) if seller else None
        service_lines = [
            line
            for line in keyword_lines
            if (
                re.match(r"\s*(?:(?:项目|商品|服务)名称|[*＊])", line.text)
                or not any(
                    label in line.text
                    for label in ("公司", "名称", "销售方", "销方", "开票方", "纳税人")
                )
            )
            and not (seller_text and "".join(line.text.casefold().split()) in seller_text)
        ]
        service_text = " ".join(line.text for line in service_lines).casefold()
        keywords = tuple(rule.keyword for rule in self._keyword_rules if rule.keyword)
        transport = extract_passenger_transport_type(lines, keywords)
        passenger = is_passenger_transport_text(service_text) or transport is not None
        service_keywords = (
            extract_passenger_service_keywords(service_lines, keywords) if passenger else set()
        )
        matched: list[ReceiptKeywordRule] = []
        for rule in self._keyword_rules:
            if not rule.keyword:
                continue
            typed_transport = rule.transport_type is not None
            operator_context = passenger or any(
                any(
                    f"{''.join(rule.keyword.split())}{label}"
                    in "".join(line.text.casefold().split())
                    for label in ("服务", "发票", "行程单")
                )
                for line in service_lines
            )
            seller_match = (
                seller_text is not None
                and "".join(rule.keyword.split()) in seller_text
                and (rule.category is not ExpenseCategory.LOCAL_TRANSPORT or operator_context)
            )
            if seller_match:
                matched.append(rule)
            elif (
                typed_transport
                and seller_text is None
                and operator_context
                and "".join(rule.keyword.split()) in compact
            ):
                matched.append(rule)
            elif passenger:
                if (transport is not None and rule.keyword in transport.casefold()) or (
                    transport is None and rule.keyword in service_keywords
                ):
                    matched.append(rule)
            elif not typed_transport and rule.keyword in keyword_text:
                matched.append(rule)
        return tuple(matched)

    def parse(self, lines: list[OcrLine], context: ParseContext) -> ParsedExpense:
        text = " ".join(line.text for line in invoice_keyword_lines(lines))
        category, needs_review = self._classification(lines)
        transport_invoice = is_passenger_transport_text(text) or category in {
            ExpenseCategory.LOCAL_TRANSPORT,
            ExpenseCategory.RAIL_FARE,
            ExpenseCategory.AIRFARE,
        }
        if transport_invoice:
            parsed_date = extract_passenger_occurrence_date(lines, context.reference_year)
        else:
            parsed_date = extract_date(lines, context.reference_year, ("开票日期",))
        qr_amount = context.invoice_qr.amount if context.invoice_qr else None
        amount, qr_warnings = extract_invoice_amount(lines, qr_amount)
        if parsed_date is None and context.invoice_qr and context.invoice_qr.issue_date:
            parsed_date = context.invoice_qr.issue_date
            qr_warnings = (*qr_warnings, QR_ISSUE_DATE_USED)
        route = extract_route(lines)
        warnings = _warnings(lines, amount=amount, parsed_date=parsed_date)
        warnings.extend(qr_warnings)
        if transport_invoice and uses_invoice_date_as_occurrence(
            lines,
            context.reference_year,
            parsed_date,
            (PASSENGER_OCCURRENCE_DATE_PREFIX.removesuffix("："), "出行日期", "乘车日期"),
        ):
            warnings.append(INVOICE_DATE_USED_AS_OCCURRENCE)
        if needs_review or category is ExpenseCategory.OTHER:
            warnings.append(MANUAL_REVIEW_REQUIRED)
        confidence = min(average_confidence(lines), self.match(lines)) if lines else 0.0
        return ParsedExpense(
            receipt_type="invoice",
            category=category,
            date=parsed_date,
            description=route,
            amount=amount,
            confidence=confidence,
            warnings=tuple(dict.fromkeys(warnings)),
        )


class FallbackParser:
    def match(self, lines: list[OcrLine]) -> float:
        return 0.01

    def parse(self, lines: list[OcrLine], context: ParseContext) -> ParsedExpense:
        parsed_date = extract_date(lines, context.reference_year)
        qr_amount = context.invoice_qr.amount if context.invoice_qr else None
        amount, qr_warnings = extract_invoice_amount(lines, qr_amount)
        if parsed_date is None and context.invoice_qr and context.invoice_qr.issue_date:
            parsed_date = context.invoice_qr.issue_date
            qr_warnings = (*qr_warnings, QR_ISSUE_DATE_USED)
        warnings = _warnings(lines, amount=amount, parsed_date=parsed_date)
        warnings.extend(qr_warnings)
        warnings.append(MANUAL_REVIEW_REQUIRED)
        return ParsedExpense(
            receipt_type="other",
            category=ExpenseCategory.OTHER,
            date=parsed_date,
            description=None,
            amount=amount,
            confidence=min(average_confidence(lines), 0.2),
            warnings=tuple(dict.fromkeys(warnings)),
        )


class ReceiptParserRegistry:
    def __init__(
        self,
        parsers: tuple[BaseReceiptParser, ...] | None = None,
        keyword_rules: tuple[ReceiptKeywordRule, ...] | None = None,
    ) -> None:
        rules = DEFAULT_RECEIPT_KEYWORD_RULES if keyword_rules is None else keyword_rules
        self._parsers = (
            (
                ForeignReceiptParser(),
                PhysicalTaxiReceiptParser(),
                TrainTicketParser(),
                GenericInvoiceParser(keyword_rules=rules),
            )
            if parsers is None
            else parsers
        )
        self._fallback = FallbackParser()
        self._keyword_rules = rules

    def _apply_keyword_fallback(
        self,
        parsed: ParsedExpense,
        lines: list[OcrLine],
    ) -> ParsedExpense:
        if parsed.category is not ExpenseCategory.OTHER or not self._keyword_rules:
            return parsed
        raw_text = " ".join(line.text for line in invoice_keyword_lines(lines))
        if is_passenger_transport_text(raw_text):
            return parsed
        matches = {
            rule.category
            for rule in GenericInvoiceParser(keyword_rules=self._keyword_rules).matching_rules(
                lines
            )
        }
        if len(matches) != 1:
            return parsed
        category = next(iter(matches))
        warnings = tuple(
            warning
            for warning in parsed.warnings
            if warning != MANUAL_REVIEW_REQUIRED or parsed.receipt_type == "foreign_receipt"
        )
        return replace(parsed, category=category, warnings=warnings)

    def parse(
        self, lines: list[OcrLine], context: ParseContext, *, trace: dict[str, object] | None = None
    ) -> ParsedExpense:
        parser = max(self._parsers, key=lambda candidate: candidate.match(lines))
        selected = self._fallback if parser.match(lines) < 0.2 else parser
        parsed = selected.parse(lines, context)
        base_category = parsed.category
        parsed = _with_transport_evidence(
            self._apply_keyword_fallback(parsed, lines), lines, self._keyword_rules
        )
        if trace is not None:
            matches = GenericInvoiceParser(keyword_rules=self._keyword_rules).matching_rules(lines)
            keyword_text = "\n".join(line.text for line in invoice_keyword_lines(lines))
            categories = {rule.category for rule in matches}
            policy = (
                "keyword_match"
                if base_category is not ExpenseCategory.OTHER
                and isinstance(selected, GenericInvoiceParser)
                else "specialized_parser"
                if base_category is not ExpenseCategory.OTHER
                else "keyword_fallback"
                if parsed.category is not ExpenseCategory.OTHER
                else "keyword_conflict"
                if len(categories) > 1
                else "passenger_guard"
                if is_passenger_transport_text(keyword_text)
                else "no_keyword_match"
            )
            trace.update(
                {
                    "parser": type(selected).__name__,
                    "parserScores": [
                        {"parser": type(item).__name__, "score": round(item.match(lines), 4)}
                        for item in self._parsers
                    ],
                    "selectionThreshold": 0.2,
                    "baseCategory": base_category.value,
                    "finalCategory": parsed.category.value,
                    "policy": policy,
                    "configuredKeywordCount": len(self._keyword_rules),
                    "matchedRules": [
                        {
                            "keyword": rule.keyword,
                            "category": rule.category.value,
                            "categoryName": CATEGORY_BY_ID[rule.category].name,
                            "transportType": rule.transport_type,
                        }
                        for rule in matches
                    ],
                    "classificationText": keyword_text[:100000],
                    "classificationTextTruncated": len(keyword_text) > 100000,
                }
            )
        invoice_numbers, order_numbers = extract_document_numbers(lines)
        return replace(
            parsed,
            invoice_numbers=invoice_numbers,
            order_numbers=order_numbers,
            rail_type=extract_rail_type(
                lines,
                railway_evidence=parsed.receipt_type == "train" or parsed.transport_type == "rail",
            ),
        )

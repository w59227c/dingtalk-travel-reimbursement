"""Bounded, in-memory evidence for an explicit administrator diagnosis only."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Literal

TEXT_LIMIT = 100_000


class RecognitionDiagnostics:
    def __init__(self) -> None:
        self._extractions: list[dict[str, object]] = []
        self._derived: list[dict[str, object]] = []
        self._seen_derived: set[tuple[int, str]] = set()
        self._raw_remaining = TEXT_LIMIT
        self._derived_remaining = TEXT_LIMIT

    def record_extraction(
        self,
        page: int,
        source: Literal["pdf_text", "paddle"],
        text: str,
        *,
        used_for_parsing: bool,
    ) -> None:
        if not text:
            return
        displayed = text[: self._raw_remaining]
        self._raw_remaining -= len(displayed)
        self._extractions.append(
            {
                "page": page,
                "source": source,
                "text": displayed,
                "truncated": len(displayed) < len(text),
                "usedForParsing": used_for_parsing,
            }
        )

    def record_derived(self, page: int, before: Iterable[str], after: Iterable[str]) -> None:
        remaining = Counter(before)
        for text in after:
            if remaining[text]:
                remaining[text] -= 1
                continue
            if (page, text) in self._seen_derived:
                continue
            self._seen_derived.add((page, text))
            displayed = text[: self._derived_remaining]
            self._derived_remaining -= len(displayed)
            self._derived.append(
                {
                    "page": page,
                    "text": displayed,
                    "truncated": len(displayed) < len(text),
                }
            )

    def as_dict(self, *, complete: bool) -> dict[str, object]:
        raw = "\n".join(str(item["text"]) for item in self._extractions)
        return {
            "recognizedText": raw[:TEXT_LIMIT],
            "recognizedTextTruncated": len(raw) > TEXT_LIMIT
            or any(item["truncated"] for item in self._extractions),
            "sources": sorted({str(item["source"]) for item in self._extractions}),
            "complete": complete,
            "extractions": self._extractions,
            "derivedFields": self._derived,
            "derivedFieldsTruncated": any(item["truncated"] for item in self._derived),
        }

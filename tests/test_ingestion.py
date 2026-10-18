"""
tests/test_ingestion.py
───────────────────────
Ingestion-focused unit tests for the two most failure-prone pieces:

- HTML parsing must not crash and must yield real sections + tables
  (the whole FAQ / V2-summary / prompt-injection trap lives in HTML).
- Holiday extraction must pull structured tables out of PDF / DOCX / HTML
  calendars, normalise dates, and ignore non-holiday (metadata or restricted)
  tables.

These operate on the real `data/corpus/` sample corpus shipped with the task.
"""
from __future__ import annotations

import os

import pytest

CORPUS = os.path.join(os.path.dirname(__file__), "..", "data", "corpus")


# ─── HTML parser ─────────────────────────────────────────────────────────────

class TestHTMLParser:
    def test_all_html_docs_parse_without_error(self):
        from src.ingestion.parsers.html_parser import parse_html
        for name in os.listdir(CORPUS):
            if name.endswith((".html", ".htm")):
                parsed = parse_html(os.path.join(CORPUS, name))
                assert parsed.sections, f"{name} produced no sections"
                assert parsed.full_text.strip(), f"{name} produced no text"

    def test_calendar_table_is_extracted(self):
        from src.ingestion.parsers.html_parser import parse_html
        parsed = parse_html(os.path.join(CORPUS, "holiday_calendar_us_2026.html"))
        tables = [s for s in parsed.sections if s["is_table"]]
        assert tables, "expected at least one table section"
        # The holiday table must contain a known US holiday name from the sample corpus.
        combined = "\n".join(t["text"] for t in tables)
        assert "Thanksgiving" in combined


# ─── Scanned-PDF OCR (pip-only engine, no system binaries) ───────────────────

class TestScannedPdfOcr:
    def test_scanned_memo_is_detected_and_ocrd(self):
        """
        The hybrid-work memo is an image-only PDF. It must be detected as scanned
        and recovered via the pip-installed OCR engine — no Tesseract/Poppler.

        Skipped (not failed) when the OCR engine is not installed, so the suite
        stays runnable in a bare environment.
        """
        import pytest as _pytest
        _pytest.importorskip("rapidocr_onnxruntime", reason="pip-only OCR engine not installed")

        from src.ingestion.parsers.pdf_parser import parse_pdf
        memo = os.path.join(CORPUS, "memo_hybrid_work_update.pdf")
        parsed = parse_pdf(memo)

        assert parsed.used_ocr, "scanned memo was not flagged for OCR"
        assert parsed.full_text.strip(), "OCR produced no text for the scanned memo"
        # Sanity: the memo is about hybrid work and mentions the 3-day WFH change.
        compact = parsed.full_text.replace(" ", "").lower()
        assert "hybrid" in compact and "3days" in compact


# ─── Date normalisation ──────────────────────────────────────────────────────

class TestNormalizeDate:
    @pytest.mark.parametrize("raw,expected", [
        ("26 January 2026", "2026-01-26"),
        ("2026-01-26", "2026-01-26"),
        ("1 May 2026", "2026-05-01"),
        ("December 25, 2026", "2026-12-25"),
        ("25 December 2026", "2026-12-25"),
        ("15 Aug", "2026-08-15"),           # no year → default 2026
        ("not a date", None),
    ])
    def test_normalize_date(self, raw, expected):
        from src.ingestion.holiday_extractor import normalize_date
        assert normalize_date(raw, 2026) == expected

    def test_normalize_uses_doc_year(self):
        from src.ingestion.holiday_extractor import normalize_date
        assert normalize_date("14 January", 2025) == "2025-01-14"


# ─── Holiday table parsing ───────────────────────────────────────────────────

class TestParseHolidayTable:
    def test_metadata_table_is_ignored(self):
        from src.ingestion.holiday_extractor import parse_holiday_table
        rows = [
            ["Document ID", "CAL-HOL-US-2026"],
            ["Version", "2026"],
            ["Effective date", "1 January 2026"],
        ]
        assert parse_holiday_table(rows, "CAL-HOL-US-2026", "US") == []

    def test_restricted_holiday_table_is_skipped(self):
        from src.ingestion.holiday_extractor import parse_holiday_table
        rows = [
            ["Date", "Day", "Restricted holiday"],
            ["4 March 2026", "Wednesday", "Holi"],
        ]
        assert parse_holiday_table(rows, "CAL-HOL-IN-2026", "IN") == []

    def test_public_holiday_table_is_parsed(self):
        from src.ingestion.holiday_extractor import parse_holiday_table
        rows = [
            ["Date", "Day", "Holiday"],
            ["26 January 2026", "Monday", "Republic Day"],
            ["15 August 2026", "Saturday", "Independence Day"],
        ]
        result = parse_holiday_table(rows, "CAL-HOL-IN-2026", "IN")
        assert [h["date"] for h in result] == ["2026-01-26", "2026-08-15"]
        assert result[0]["name"] == "Republic Day"
        assert result[0]["region"] == "IN"


# ─── End-to-end holiday extraction from the real sample corpus ────────────────

@pytest.mark.usefixtures("override_db")
class TestHolidayExtraction:
    @pytest.mark.parametrize("filename,doc_id,region,min_holidays,expected", [
        ("holiday_calendar_india_2026.pdf", "CAL-HOL-IN-2026", "IN", 10, ("2026-01-26", "Republic Day")),
        ("holiday_calendar_us_2026.html", "CAL-HOL-US-2026", "US", 8, ("2026-07-03", None)),
        ("holiday_calendar_germany_2026.docx", "CAL-HOL-DE-2026", "DE", 8, ("2026-10-03", None)),
    ])
    def test_extracts_calendar_holidays(self, filename, doc_id, region, min_holidays, expected):
        from src.ingestion.holiday_extractor import extract_holidays_from_document
        from src.data_access.holiday_repo import get_holiday_dates_for_region

        count = extract_holidays_from_document(
            os.path.join(CORPUS, filename), {"doc_id": doc_id, "regions": [region]}
        )
        assert count >= min_holidays

        dates = get_holiday_dates_for_region(region, 2026)
        assert expected[0] in dates

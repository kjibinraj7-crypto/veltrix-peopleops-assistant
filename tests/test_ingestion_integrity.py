"""
tests/test_ingestion_integrity.py
─────────────────────────────────
Tests for the ingestion integrity checks:
  * a page with plenty of text is OK
  * a thin/empty page is FLAGGED (not silently dropped)
  * a page recovered by OCR is recorded as such
  * a document with no chunks, or too little text, is FLAGGED
  * the report surfaces every flag and writes a JSON file
"""
from __future__ import annotations

import json

from src.ingestion.integrity import (
    FLAG_EMPTY,
    FLAG_THIN,
    OCR_RECOVERED,
    OK,
    IngestionReport,
    classify_document,
    classify_page,
)


def test_page_with_enough_text_is_ok():
    f = classify_page("a.pdf", "A", 1, chars_extracted=900, chars_final=900,
                      used_ocr=False, min_page_chars=200)
    assert f.status == OK


def test_empty_page_is_flagged_not_dropped():
    f = classify_page("a.pdf", "A", 2, chars_extracted=0, chars_final=0,
                      used_ocr=True, min_page_chars=200)
    assert f.status == FLAG_EMPTY


def test_thin_page_is_flagged():
    f = classify_page("a.pdf", "A", 3, chars_extracted=40, chars_final=45,
                      used_ocr=True, min_page_chars=200)
    assert f.status == FLAG_THIN


def test_page_recovered_by_ocr_is_recorded():
    f = classify_page("a.pdf", "A", 4, chars_extracted=10, chars_final=800,
                      used_ocr=True, min_page_chars=200)
    assert f.status == OCR_RECOVERED


def test_document_with_no_chunks_is_flagged():
    f = classify_document("b.docx", "B", total_chars=5000, chunks=0, min_document_chars=200)
    assert f.status == FLAG_EMPTY


def test_thin_document_is_flagged():
    f = classify_document("c.html", "C", total_chars=30, chunks=1, min_document_chars=200)
    assert f.status == FLAG_THIN


def test_report_summarises_and_saves(tmp_path):
    report = IngestionReport()
    report.add_page(classify_page("a.pdf", "A", 1, 900, 900, False, 200))
    report.add_page(classify_page("a.pdf", "A", 2, 0, 0, True, 200))
    report.add_document(classify_document("a.pdf", "A", 900, 1, 200))
    report.add_missing_file("gone.pdf")

    assert report.has_flags()
    summary = report.summary()
    assert summary["pages_checked"] == 2
    assert summary["flagged_pages"] == 1
    assert summary["missing_files"] == 1

    report.save(str(tmp_path))
    saved = json.loads((tmp_path / "ingestion_report.json").read_text(encoding="utf-8"))
    assert saved["summary"]["flagged_pages"] == 1
    assert saved["missing_files"] == ["gone.pdf"]
    assert saved["flagged_pages"][0]["page"] == 2


def test_clean_report_has_no_flags():
    report = IngestionReport()
    report.add_page(classify_page("a.pdf", "A", 1, 900, 900, False, 200))
    report.add_document(classify_document("a.pdf", "A", 900, 3, 200))
    assert not report.has_flags()

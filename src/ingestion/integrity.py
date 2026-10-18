"""
src/ingestion/integrity.py
──────────────────────────
Ingestion integrity checks — make sure no page silently disappears.

For every page we run several independent checks:

  1. **Extraction check** — how many characters survived text extraction? A page
     below `min_page_chars` is treated as suspicious (not trusted).
  2. **OCR attempt** — a suspicious page is re-read with OCR.
  3. **Post-OCR check** — if OCR still yields fewer than `min_page_chars`, the
     page is FLAGGED for a human instead of being dropped.
  4. **Document check** — a document whose total usable text is below
     `min_document_chars`, or that produced no chunks, is FLAGGED.
  5. **Manifest check** — a manifest entry whose file is missing is FLAGGED.

Every flag is printed (and logged) while the embeddings are being built, and the
full report is written to `vector_store/ingestion_report.json` so the validation
team can see exactly which pages need attention.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from src.utils.logging import get_logger

logger = get_logger(__name__)

REPORT_FILENAME = "ingestion_report.json"

# Page statuses
OK = "ok"
OCR_RECOVERED = "ocr_recovered"
FLAG_EMPTY = "flagged_empty"
FLAG_THIN = "flagged_thin_text"
FLAG_PARSE_ERROR = "flagged_parse_error"

_FLAGGED = {FLAG_EMPTY, FLAG_THIN, FLAG_PARSE_ERROR}


@dataclass
class PageFinding:
    file: str
    doc_id: str
    page: int
    chars_extracted: int
    chars_final: int
    used_ocr: bool
    status: str
    detail: str = ""


@dataclass
class DocumentFinding:
    file: str
    doc_id: str
    total_chars: int
    chunks: int
    status: str
    detail: str = ""


@dataclass
class IngestionReport:
    pages_checked: int = 0
    ocr_pages: int = 0
    ocr_recovered: int = 0
    pages: list[PageFinding] = field(default_factory=list)
    documents: list[DocumentFinding] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    parse_errors: list[str] = field(default_factory=list)

    # ── recording ─────────────────────────────────────────────────────────────

    def add_page(self, finding: PageFinding) -> None:
        self.pages_checked += 1
        if finding.used_ocr:
            self.ocr_pages += 1
            if finding.status == OCR_RECOVERED:
                self.ocr_recovered += 1
        self.pages.append(finding)

    def add_document(self, finding: DocumentFinding) -> None:
        self.documents.append(finding)

    def add_missing_file(self, file_name: str) -> None:
        self.missing_files.append(file_name)

    def add_parse_error(self, file_name: str, error: str) -> None:
        self.parse_errors.append(f"{file_name}: {error}")

    # ── reporting ─────────────────────────────────────────────────────────────

    @property
    def flagged_pages(self) -> list[PageFinding]:
        return [p for p in self.pages if p.status in _FLAGGED]

    @property
    def flagged_documents(self) -> list[DocumentFinding]:
        return [d for d in self.documents if d.status in _FLAGGED]

    def has_flags(self) -> bool:
        return bool(
            self.flagged_pages or self.flagged_documents
            or self.missing_files or self.parse_errors
        )

    def summary(self) -> dict[str, Any]:
        return {
            "pages_checked": self.pages_checked,
            "ocr_pages": self.ocr_pages,
            "ocr_recovered": self.ocr_recovered,
            "flagged_pages": len(self.flagged_pages),
            "documents_checked": len(self.documents),
            "flagged_documents": len(self.flagged_documents),
            "missing_files": len(self.missing_files),
            "parse_errors": len(self.parse_errors),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "flagged_pages": [
                {"file": p.file, "doc_id": p.doc_id, "page": p.page,
                 "chars_extracted": p.chars_extracted, "chars_final": p.chars_final,
                 "used_ocr": p.used_ocr, "status": p.status, "detail": p.detail}
                for p in self.flagged_pages
            ],
            "flagged_documents": [
                {"file": d.file, "doc_id": d.doc_id, "total_chars": d.total_chars,
                 "chunks": d.chunks, "status": d.status, "detail": d.detail}
                for d in self.flagged_documents
            ],
            "missing_files": self.missing_files,
            "parse_errors": self.parse_errors,
        }

    def print_report(self) -> None:
        """Print a human-readable integrity report (called at embedding time)."""
        s = self.summary()
        lines = [
            "",
            "=" * 78,
            "INGESTION INTEGRITY REPORT",
            "=" * 78,
            f"  pages checked          : {s['pages_checked']}",
            f"  pages needing OCR      : {s['ocr_pages']}  "
            f"(recovered by OCR: {s['ocr_recovered']})",
            f"  FLAGGED pages          : {s['flagged_pages']}",
            f"  documents checked      : {s['documents_checked']}",
            f"  FLAGGED documents      : {s['flagged_documents']}",
            f"  missing files          : {s['missing_files']}",
            f"  parse errors           : {s['parse_errors']}",
        ]
        if not self.has_flags():
            lines.append("  RESULT: clean — every page produced usable text.")
        else:
            lines.append("  RESULT: ATTENTION REQUIRED — items below need review:")
            for p in self.flagged_pages:
                lines.append(
                    f"    [PAGE] {p.file} p.{p.page}: {p.status} "
                    f"(extracted {p.chars_extracted} chars, final {p.chars_final}, "
                    f"ocr={p.used_ocr}) {p.detail}"
                )
            for d in self.flagged_documents:
                lines.append(
                    f"    [DOC ] {d.file}: {d.status} "
                    f"({d.total_chars} chars, {d.chunks} chunks) {d.detail}"
                )
            for f in self.missing_files:
                lines.append(f"    [FILE] missing from corpus: {f}")
            for e in self.parse_errors:
                lines.append(f"    [ERR ] {e}")
        lines.append("=" * 78)
        print("\n".join(lines), flush=True)

    def save(self, vector_store_dir: str, corpus_hash: str = "") -> None:
        path = os.path.join(vector_store_dir, REPORT_FILENAME)
        try:
            os.makedirs(vector_store_dir, exist_ok=True)
            payload = self.to_dict()
            # Stamp the corpus this report describes, so a later startup with an
            # unchanged corpus can trust the file instead of re-parsing everything.
            payload["corpus_hash"] = corpus_hash
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            logger.info("Ingestion integrity report saved", extra={"path": path})
        except Exception as exc:  # noqa: BLE001 — reporting must never break startup
            logger.warning("Could not save ingestion report", extra={"error": str(exc)})


def classify_page(
    file_name: str,
    doc_id: str,
    page_number: int,
    chars_extracted: int,
    chars_final: int,
    used_ocr: bool,
    min_page_chars: int,
) -> PageFinding:
    """Apply the page-level checks and return a classified finding."""
    if chars_final >= min_page_chars:
        status = OCR_RECOVERED if used_ocr else OK
        detail = "text recovered by OCR" if used_ocr else ""
    elif chars_final <= 0:
        status = FLAG_EMPTY
        detail = "no text extracted and OCR produced nothing"
    else:
        status = FLAG_THIN
        detail = (
            f"only {chars_final} chars (threshold {min_page_chars})"
            + (" after OCR" if used_ocr else " and OCR did not improve it")
        )
    return PageFinding(
        file=file_name, doc_id=doc_id, page=page_number,
        chars_extracted=chars_extracted, chars_final=chars_final,
        used_ocr=used_ocr, status=status, detail=detail,
    )


def classify_document(
    file_name: str, doc_id: str, total_chars: int, chunks: int, min_document_chars: int
) -> DocumentFinding:
    """Apply the document-level checks."""
    if chunks == 0:
        status, detail = FLAG_EMPTY, "document produced no chunks"
    elif total_chars < min_document_chars:
        status, detail = FLAG_THIN, f"only {total_chars} chars extracted"
    else:
        status, detail = OK, ""
    return DocumentFinding(
        file=file_name, doc_id=doc_id, total_chars=total_chars,
        chunks=chunks, status=status, detail=detail,
    )

"""
src/ingestion/parsers/pdf_parser.py
─────────────────────────────────────
PDF text extraction with multi-signal OCR detection.

OCR detection uses three signals (not just character count):
  1. Text density (chars / page area)
  2. Presence of real font/glyph blocks
  3. Embedded image presence

OCR engine (fully self-contained, no system binaries):
  - Pages are rasterised with PyMuPDF itself (no Poppler / pdf2image).
  - Text is recognised with RapidOCR, a pip-installed ONNX model
    (`rapidocr-onnxruntime`) — a local library, not an external service.
  - Tesseract is used only as an optional last-resort fallback *if* its
    binary happens to be present; it is never required.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

from src.ingestion.text_cleaner import clean_text
from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ParsedPage:
    page_number: int      # 1-indexed
    text: str
    headings: list[str] = field(default_factory=list)


@dataclass
class ParsedDocument:
    file_path: str
    pages: list[ParsedPage] = field(default_factory=list)
    used_ocr: bool = False
    # One record per physical page, including pages that yielded no text, so the
    # ingestion integrity report can flag anything that was silently lost.
    page_findings: list[dict] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return "\n\n".join(p.text for p in self.pages if p.text.strip())


def is_scanned_page(page) -> bool:
    """Multi-signal scanned page detection."""
    try:
        import fitz  # PyMuPDF
        text = page.get_text().strip()
        area = page.rect.width * page.rect.height
        text_density = len(text) / max(area, 1)

        # Signal 1: Negligible text density
        if text_density < 0.001:
            images = page.get_images(full=True)
            if images:
                return True

        # Signal 2: Short text with no real font blocks
        if len(text) < 100:
            text_dict = page.get_text("dict")
            has_real_text = any(
                block.get("lines")
                for block in text_dict.get("blocks", [])
                if block.get("type") == 0
            )
            if not has_real_text:
                return True

        return False
    except Exception:
        return False


def _detect_heading(text: str, font_size: float, max_font_size: float) -> bool:
    """Heuristic: is this text a heading based on font size?"""
    return font_size >= max_font_size * 0.85 and len(text.strip()) < 120


def parse_pdf(file_path: str, min_page_chars: int = 0) -> ParsedDocument:
    """
    Parse a PDF file. Falls back to OCR for scanned or thin pages.

    `min_page_chars` is the "suspicious page" threshold from settings: any page
    whose extracted text is shorter than this is re-read with OCR, and recorded
    in `page_findings` so the caller can flag it if OCR does not help either.
    """
    try:
        import fitz
    except ImportError:
        logger.error("PyMuPDF not installed — cannot parse PDFs")
        return ParsedDocument(file_path=file_path)

    doc_result = ParsedDocument(file_path=file_path)

    try:
        pdf = fitz.open(file_path)
        for page_num, page in enumerate(pdf, start=1):
            raw_text = _extract_text_page(page)
            chars_extracted = len(raw_text.strip())
            used_ocr = False

            # A page is suspicious when it looks scanned OR when plain extraction
            # returned suspiciously little text. Both cases get an OCR attempt.
            thin = min_page_chars > 0 and chars_extracted < min_page_chars
            if is_scanned_page(page) or thin:
                ocr_text = _ocr_page(page, file_path, page_num)
                used_ocr = True
                doc_result.used_ocr = True
                # Keep whichever extraction produced more text.
                text = ocr_text if len(ocr_text.strip()) > chars_extracted else raw_text
                if ocr_text.strip() and chars_extracted < min_page_chars:
                    logger.info(
                        "Thin PDF page recovered with OCR",
                        extra={"file": file_path, "page": page_num,
                               "chars_before": chars_extracted, "chars_after": len(ocr_text.strip())},
                    )
            else:
                text = raw_text

            cleaned = clean_text(text)
            doc_result.page_findings.append({
                "page": page_num,
                "chars_extracted": chars_extracted,
                "chars_final": len(cleaned.strip()),
                "used_ocr": used_ocr,
            })

            if cleaned:
                # Extract headings by font size
                headings = _extract_headings(page)
                doc_result.pages.append(ParsedPage(
                    page_number=page_num,
                    text=cleaned,
                    headings=headings,
                ))

        pdf.close()
        logger.info(
            "PDF parsed",
            extra={"file": file_path, "pages": len(doc_result.pages), "ocr": doc_result.used_ocr},
        )
    except Exception as exc:
        logger.error("PDF parse error", extra={"file": file_path, "error": str(exc)})

    return doc_result


def _extract_text_page(page) -> str:
    """Extract text from a text-based PDF page."""
    return page.get_text("text")


def _extract_headings(page) -> list[str]:
    """Extract heading-like text based on font size."""
    headings = []
    try:
        blocks = page.get_text("dict").get("blocks", [])
        font_sizes = [
            span["size"]
            for block in blocks if block.get("type") == 0
            for line in block.get("lines", [])
            for span in line.get("spans", [])
            if span.get("size", 0) > 0
        ]
        if not font_sizes:
            return headings
        max_size = max(font_sizes)
        for block in blocks:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if _detect_heading(span.get("text", ""), span.get("size", 0), max_size):
                        h = span["text"].strip()
                        if h and h not in headings:
                            headings.append(h)
    except Exception:
        pass
    return headings


# Module-level OCR engine singleton. RapidOCR loads ONNX models on first use,
# so we keep one instance for the whole process instead of rebuilding per page.
_RAPIDOCR_ENGINE: Any = None


def _get_rapidocr_engine():
    """Lazily initialise the pip-installed RapidOCR engine (ONNX, no binaries)."""
    global _RAPIDOCR_ENGINE
    if _RAPIDOCR_ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        _RAPIDOCR_ENGINE = RapidOCR()
    return _RAPIDOCR_ENGINE


def _render_page_to_numpy(page, zoom: float = 2.0):
    """
    Rasterise a PDF page to an RGB numpy array using PyMuPDF only.

    This removes the Poppler dependency that `pdf2image` would otherwise add.
    `zoom=2.0` renders at ~144 dpi, which is enough for clean OCR while keeping
    memory use modest.
    """
    import fitz
    import numpy as np

    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:  # drop an unexpected alpha channel
        img = img[:, :, :3]
    return img


def _ocr_with_rapidocr(image) -> str:
    """Run RapidOCR over a numpy image and join the recognised lines."""
    engine = _get_rapidocr_engine()
    result, _elapsed = engine(image)
    if not result:
        return ""
    # Each entry is [bounding_box, text, confidence]; keep the text, top-to-bottom.
    return "\n".join(line[1] for line in result if len(line) >= 2 and line[1])


def _ocr_page(page, file_path: str, page_num: int) -> str:
    """
    Apply OCR to a single scanned page.

    Order of preference:
      1. RapidOCR (pip-only ONNX model) — the supported path, no system binaries.
      2. Tesseract — only if installed on the host; never required.
    Returns "" if no engine can read the page, so ingestion degrades gracefully.
    """
    try:
        image = _render_page_to_numpy(page)
    except Exception as exc:
        logger.warning(
            "OCR page rasterisation failed",
            extra={"file": file_path, "page": page_num, "error": str(exc)},
        )
        return ""

    # ── Primary engine: RapidOCR (pip-installed, no external binaries) ────────
    try:
        text = _ocr_with_rapidocr(image)
        if text.strip():
            logger.info("OCR applied (RapidOCR)", extra={"file": file_path, "page": page_num})
            return text
    except Exception as exc:
        logger.warning(
            "RapidOCR unavailable or failed",
            extra={"file": file_path, "page": page_num, "error": str(exc)},
        )

    # ── Optional fallback: Tesseract, only if present on the host ─────────────
    try:
        import pytesseract
        from PIL import Image

        text = pytesseract.image_to_string(Image.fromarray(image), lang="eng")
        if text.strip():
            logger.info("OCR applied (Tesseract fallback)", extra={"file": file_path, "page": page_num})
            return text
    except Exception:
        pass

    logger.warning("OCR produced no text", extra={"file": file_path, "page": page_num})
    return ""

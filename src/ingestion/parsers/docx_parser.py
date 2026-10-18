"""
src/ingestion/parsers/docx_parser.py
──────────────────────────────────────
DOCX parser using python-docx.
Preserves paragraph + table order by iterating the raw XML body.
Tables are formatted as markdown tables for embedding.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from src.ingestion.text_cleaner import clean_text, clean_table_cell
from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ParsedDocx:
    file_path: str
    sections: list[dict] = field(default_factory=list)  # [{heading, text, is_table}]

    @property
    def full_text(self) -> str:
        return "\n\n".join(s["text"] for s in self.sections if s["text"].strip())


def _table_to_markdown(table) -> str:
    """Convert a python-docx Table to a markdown table string."""
    rows = []
    for row in table.rows:
        cells = [clean_table_cell(cell.text) for cell in row.cells]
        rows.append("| " + " | ".join(cells) + " |")
    if not rows:
        return ""
    # Add header separator after first row
    sep = "| " + " | ".join(["---"] * len(table.rows[0].cells)) + " |"
    rows.insert(1, sep)
    return "\n".join(rows)


def parse_docx(file_path: str) -> ParsedDocx:
    """Parse a DOCX file preserving body order (paragraphs + tables)."""
    try:
        from docx import Document
        from docx.oxml.ns import qn
    except ImportError:
        logger.error("python-docx not installed")
        return ParsedDocx(file_path=file_path)

    result = ParsedDocx(file_path=file_path)

    try:
        doc = Document(file_path)
        current_heading = ""
        current_text_parts: list[str] = []

        def flush():
            nonlocal current_text_parts
            text = clean_text("\n".join(current_text_parts))
            if text.strip():
                result.sections.append({"heading": current_heading, "text": text, "is_table": False})
            current_text_parts = []

        for child in doc.element.body:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "p":
                # Paragraph
                from docx.oxml.ns import qn as _qn
                style = child.find(f".//{{{child.nsmap.get('w', 'http://schemas.openxmlformats.org/wordprocessingml/2006/main')}}}pStyle")
                style_val = style.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val", "") if style is not None else ""
                text_parts = [node.text or "" for node in child.iter() if node.tag.endswith("}t")]
                para_text = "".join(text_parts).strip()

                if not para_text:
                    continue

                if "Heading" in style_val or style_val.startswith("h"):
                    flush()
                    current_heading = para_text
                else:
                    current_text_parts.append(para_text)

            elif tag == "tbl":
                # Table — use python-docx Table wrapper for easier access
                flush()
                from docx.table import Table as DocxTable
                from docx.oxml import OxmlElement
                try:
                    tbl = DocxTable(child, doc)
                    md = _table_to_markdown(tbl)
                    if md.strip():
                        result.sections.append({"heading": current_heading, "text": md, "is_table": True})
                except Exception:
                    pass

        flush()
        logger.info("DOCX parsed", extra={"file": file_path, "sections": len(result.sections)})
    except Exception as exc:
        logger.error("DOCX parse error", extra={"file": file_path, "error": str(exc)})

    return result

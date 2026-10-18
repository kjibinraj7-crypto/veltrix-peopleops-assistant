"""
src/ingestion/parsers/html_parser.py
──────────────────────────────────────
HTML parser using BeautifulSoup4.
Extracts structured text + tables in document order.
Strips scripts, styles, and HTML boilerplate.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from src.ingestion.text_cleaner import clean_text, clean_table_cell
from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ParsedHTML:
    file_path: str
    sections: list[dict] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return "\n\n".join(s["text"] for s in self.sections if s["text"].strip())


def _table_to_markdown(table_tag) -> str:
    """Convert a BeautifulSoup table element to markdown."""
    rows = []
    for tr in table_tag.find_all("tr"):
        cells = [clean_table_cell(td.get_text()) for td in tr.find_all(["td", "th"])]
        if cells:
            rows.append("| " + " | ".join(cells) + " |")
    if not rows:
        return ""
    # Insert separator after first row (header)
    sep = "| " + " | ".join(["---"] * len(rows[0].split("|")[1:-1])) + " |"
    rows.insert(1, sep)
    return "\n".join(rows)


def parse_html(file_path: str) -> ParsedHTML:
    """Parse an HTML file, extracting headings, paragraphs, and tables."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.error("beautifulsoup4 not installed")
        return ParsedHTML(file_path=file_path)

    result = ParsedHTML(file_path=file_path)

    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            content = f.read()

        soup = BeautifulSoup(content, "lxml")

        # Remove noise elements
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        current_heading = ""
        current_text_parts: list[str] = []

        def flush():
            nonlocal current_text_parts
            text = clean_text("\n".join(current_text_parts))
            if text.strip():
                result.sections.append({"heading": current_heading, "text": text, "is_table": False})
            current_text_parts = []

        body = soup.find("body") or soup
        for element in body.descendants:
            # NavigableStrings and comments have no usable tag name
            name = getattr(element, "name", None)
            if not name:
                continue
            name = name.lower()

            # Elements nested inside a table are already emitted as part of the
            # markdown table when the <table> was processed — skip them here.
            # (Using find_parent avoids mutating the parse tree, which breaks
            # BeautifulSoup for NavigableString descendants.)
            if name != "table" and element.find_parent("table") is not None:
                continue

            if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                flush()
                current_heading = element.get_text(strip=True)
            elif name == "table":
                flush()
                md = _table_to_markdown(element)
                if md.strip():
                    result.sections.append({"heading": current_heading, "text": md, "is_table": True})
            elif name in ("p", "li", "div") and not element.find(["table", "h1", "h2", "h3"]):
                text = element.get_text(separator=" ", strip=True)
                if text:
                    current_text_parts.append(text)

        flush()
        logger.info("HTML parsed", extra={"file": file_path, "sections": len(result.sections)})

    except Exception as exc:
        logger.error("HTML parse error", extra={"file": file_path, "error": str(exc)})

    return result

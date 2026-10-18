"""
src/ingestion/parsers/markdown_parser.py
──────────────────────────────────────────
Markdown parser — direct read with heading structure detection.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from src.ingestion.text_cleaner import clean_text
from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ParsedMarkdown:
    file_path: str
    sections: list[dict] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return "\n\n".join(s["text"] for s in self.sections if s["text"].strip())


_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)$')


def parse_markdown(file_path: str) -> ParsedMarkdown:
    """Parse a Markdown file, splitting into sections by heading."""
    result = ParsedMarkdown(file_path=file_path)

    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            content = f.read()

        lines = content.split("\n")
        current_heading = ""
        current_lines: list[str] = []

        def flush():
            nonlocal current_lines
            text = clean_text("\n".join(current_lines))
            if text.strip():
                result.sections.append({"heading": current_heading, "text": text, "is_table": False})
            current_lines = []

        for line in lines:
            m = _HEADING_RE.match(line)
            if m:
                flush()
                current_heading = m.group(2).strip()
            else:
                current_lines.append(line)

        flush()
        logger.info("Markdown parsed", extra={"file": file_path, "sections": len(result.sections)})

    except Exception as exc:
        logger.error("Markdown parse error", extra={"file": file_path, "error": str(exc)})

    return result

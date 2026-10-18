"""
src/ingestion/text_cleaner.py
──────────────────────────────
Normalize parsed text before chunking and embedding.
Removes noise (page numbers, excessive whitespace, repeated headers/footers)
without removing any policy content.
"""
from __future__ import annotations
import re


def clean_text(raw: str) -> str:
    """
    Clean parsed document text. Rules:
    - Collapse 3+ blank lines to 2
    - Normalize horizontal whitespace
    - Remove page number artifacts ("Page 3 of 12", lone digits)
    - Remove common footer/header patterns (Confidential, ©)
    - Strip leading/trailing whitespace
    DO NOT remove: numbers, dates, policy text, section headings.
    """
    if not raw:
        return ""
    text = raw
    # Remove page number artifacts
    text = re.sub(r'(?i)\bpage\s+\d+\s+(of\s+\d+)?\b', '', text)
    # Remove lone line numbers (single number on its own line)
    text = re.sub(r'^\s*\d{1,3}\s*$', '', text, flags=re.MULTILINE)
    # Remove common footer patterns
    text = re.sub(r'(?i)confidential\s*[|–-]?\s*internal\s*use\s*only', '', text)
    text = re.sub(r'©\s*\d{4}.*?(?=\n|$)', '', text)
    # Normalize horizontal whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    # Collapse excessive blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def clean_table_cell(cell: str) -> str:
    """Clean a single table cell value."""
    return re.sub(r'\s+', ' ', cell or '').strip()

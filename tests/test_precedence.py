"""tests/test_precedence.py — Precedence engine unit tests."""
from __future__ import annotations
import pytest
from src.retrieval.precedence import authority_rank, scope_rank, sort_chunks_by_precedence


def _chunk(doc_type, regions, effective_date, text="sample"):
    return {"text": text, "metadata": {
        "doc_id": f"DOC-{doc_type}", "doc_type": doc_type,
        "regions": regions, "effective_date": effective_date,
        "access_level": "all", "section_heading": "",
    }}


class TestAuthorityRank:
    def test_policy_beats_faq(self):
        assert authority_rank("policy") > authority_rank("faq")

    def test_policy_beats_wiki(self):
        assert authority_rank("policy") > authority_rank("wiki")

    def test_addendum_same_as_policy(self):
        assert authority_rank("addendum") == authority_rank("policy")

    def test_faq_beats_wiki(self):
        assert authority_rank("faq") > authority_rank("wiki")

    def test_unknown_is_lowest(self):
        assert authority_rank("unknown") == 0


class TestScopeRank:
    def test_region_specific_beats_global(self):
        assert scope_rank(["IN"], "IN") > scope_rank(["GLOBAL"], "IN")

    def test_wrong_region_no_scope_bonus(self):
        assert scope_rank(["US"], "IN") == 0


class TestSortByPrecedence:
    def test_policy_sorted_before_faq(self):
        chunks = [
            _chunk("faq", ["GLOBAL"], "2026-05-01"),
            _chunk("policy", ["GLOBAL"], "2026-04-01"),
        ]
        sorted_chunks = sort_chunks_by_precedence(chunks, "IN")
        assert sorted_chunks[0]["metadata"]["doc_type"] == "policy"

    def test_region_specific_beats_global_same_authority(self):
        chunks = [
            _chunk("policy", ["GLOBAL"], "2026-04-01"),
            _chunk("policy", ["IN"], "2026-04-01"),
        ]
        sorted_chunks = sort_chunks_by_precedence(chunks, "IN")
        assert "IN" in sorted_chunks[0]["metadata"]["regions"]

    def test_newer_beats_older_same_authority_and_scope(self):
        chunks = [
            _chunk("policy", ["GLOBAL"], "2025-01-01"),
            _chunk("policy", ["GLOBAL"], "2026-04-01"),
        ]
        sorted_chunks = sort_chunks_by_precedence(chunks, "IN")
        assert sorted_chunks[0]["metadata"]["effective_date"] == "2026-04-01"

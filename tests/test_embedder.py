"""
tests/test_embedder.py
──────────────────────
BGE query-prefix regression test (plan §4.2 / Phase 6.40).

Documents must be embedded WITHOUT the prefix; queries must be embedded WITH the
prefix "Represent this sentence for searching relevant passages: ". Forgetting
the query prefix silently degrades retrieval quality, so we assert it here
without loading the (large) real model.
"""
from __future__ import annotations

import numpy as np

import src.ingestion.embedder as embedder


class _FakeModel:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def encode(self, texts, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(list(texts))
        return np.zeros((len(texts), 384), dtype="float32")


def test_documents_embedded_without_prefix(monkeypatch):
    fake = _FakeModel()
    monkeypatch.setattr(embedder, "get_model", lambda: fake)
    embedder.embed_documents(["hello world"])
    assert fake.calls[0] == ["hello world"]


def test_query_embedded_with_bge_prefix(monkeypatch):
    fake = _FakeModel()
    monkeypatch.setattr(embedder, "get_model", lambda: fake)
    embedder.embed_query("how many sick days")
    sent = fake.calls[0][0]
    assert sent.startswith(embedder.BGE_QUERY_PREFIX)
    assert "how many sick days" in sent

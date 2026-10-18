"""
tests/test_confirmation.py
──────────────────────────
Unit tests for the two-step confirmation flow phrasing.

The confirmation check is the last line of defence against a write action being
executed without explicit consent, so it must be precise: clear confirmations
must be recognised, while a change-of-details request must NOT be mistaken for a
confirmation (it should fall through and produce a fresh proposal).
"""
from __future__ import annotations

from src.orchestration.confirmation import is_confirmation, is_cancellation


class TestIsConfirmation:
    def test_clear_confirmations(self):
        for phrase in ("yes", "Yes", "confirm", "go ahead", "proceed", "ok", "sure", "submit"):
            assert is_confirmation(phrase) is True, phrase

    def test_not_confirmation(self):
        for phrase in ("no", "maybe", "what?", "make it until Friday", "show my balance"):
            assert is_confirmation(phrase) is False, phrase

    def test_trailing_punctuation_ignored(self):
        assert is_confirmation("Yes!") is True
        assert is_confirmation("confirm.") is True


class TestIsCancellation:
    def test_explicit_cancellations(self):
        for phrase in ("no", "cancel", "never mind", "stop", "forget it", "discard"):
            assert is_cancellation(phrase) is True, phrase

    def test_change_request_is_not_a_bare_cancellation(self):
        # "no, make it Friday" changes details → must fall through, not be treated
        # as a pure cancellation.
        assert is_cancellation("no, make it Friday") is False

    def test_word_boundary_no_false_positive(self):
        # "know"/"now" contain "no" but must not be flagged.
        assert is_cancellation("I want to know my balance") is False
        assert is_cancellation("what now") is False

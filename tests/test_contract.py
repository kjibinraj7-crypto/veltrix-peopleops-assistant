"""
tests/test_contract.py
──────────────────────
Unit tests for the API response contract (task §8.1), citation validation
(task §4.2), PII scrubbing (task §7.4) and provider configuration.

These are pure-function tests — no DB, no LLM, no network.
"""
from __future__ import annotations

import pytest

from src.orchestration.router import (
    VALID_ROUTES,
    _call_llm,
    _enforce_route_contract,
    _is_rate_limit_error,
    _normalise_citations,
    _rate_limit_wait_seconds,
)
from src.security.pii_filter import mask_emails_in_text, strip_phones_in_text


class TestRouteContract:
    """route / citations / tool_calls must always form a legal combination."""

    def test_rag_without_citations_becomes_no_answer(self):
        assert _enforce_route_contract("rag", [], [])[0] == "no_answer"

    def test_tool_without_tool_calls_becomes_no_answer(self):
        assert _enforce_route_contract("tool", [], [])[0] == "no_answer"

    def test_hybrid_downgrades_to_rag(self):
        route, cit, tc = _enforce_route_contract("hybrid", [{"doc_id": "A"}], [])
        assert (route, len(cit), len(tc)) == ("rag", 1, 0)

    def test_hybrid_downgrades_to_tool(self):
        route, cit, tc = _enforce_route_contract("hybrid", [], [{"name": "x"}])
        assert (route, len(cit), len(tc)) == ("tool", 0, 1)

    def test_rag_with_a_tool_call_becomes_hybrid(self):
        assert _enforce_route_contract("rag", [{"doc_id": "A"}], [{"name": "x"}])[0] == "hybrid"

    def test_tool_with_a_citation_becomes_hybrid(self):
        assert _enforce_route_contract("tool", [{"doc_id": "A"}], [{"name": "x"}])[0] == "hybrid"

    def test_refuse_carries_nothing(self):
        route, cit, tc = _enforce_route_contract("refuse", [{"doc_id": "A"}], [{"name": "x"}])
        assert (route, cit, tc) == ("refuse", [], [])

    def test_clarify_carries_nothing(self):
        route, cit, tc = _enforce_route_contract("clarify", [{"doc_id": "A"}], [{"name": "x"}])
        assert (route, cit, tc) == ("clarify", [], [])

    def test_no_answer_carries_nothing(self):
        route, cit, tc = _enforce_route_contract("no_answer", [{"doc_id": "A"}], [])
        assert (route, cit, tc) == ("no_answer", [], [])

    def test_unknown_route_is_derived(self):
        assert _enforce_route_contract("banana", [], [{"name": "x"}])[0] == "tool"
        assert _enforce_route_contract("banana", [{"doc_id": "A"}], [])[0] == "rag"
        assert _enforce_route_contract(None, [], [])[0] == "no_answer"

    def test_every_combination_is_self_consistent(self):
        """Exhaustive: no input can produce a response that breaks the table."""
        for route in list(VALID_ROUTES) + ["bogus", None]:
            for cit in ([], [{"doc_id": "A"}]):
                for tc in ([], [{"name": "x"}]):
                    r, c, t = _enforce_route_contract(route, cit, tc)
                    assert r in VALID_ROUTES
                    if r == "rag":
                        assert c and not t
                    elif r == "tool":
                        assert t and not c
                    elif r == "hybrid":
                        assert c and t
                    else:
                        assert not c and not t


class TestCitationNormalisation:
    CHUNKS = [
        {"metadata": {"doc_id": "A", "section_heading": "Sick Leave", "page_number": 3}},
        {"metadata": {"doc_id": "B", "section_heading": "", "page_number": 7}},
    ]

    def test_unknown_doc_id_is_dropped(self):
        assert _normalise_citations([{"doc_id": "Z", "locator": "x"}], self.CHUNKS) == []

    def test_locator_filled_from_section_heading(self):
        assert _normalise_citations([{"doc_id": "A"}], self.CHUNKS) == [
            {"doc_id": "A", "locator": "Sick Leave"}
        ]

    def test_locator_falls_back_to_page_number(self):
        assert _normalise_citations([{"doc_id": "B", "locator": "   "}], self.CHUNKS) == [
            {"doc_id": "B", "locator": "p. 7"}
        ]

    def test_duplicate_doc_ids_removed(self):
        out = _normalise_citations([{"doc_id": "A"}, {"doc_id": "A"}], self.CHUNKS)
        assert len(out) == 1

    def test_malformed_entries_ignored(self):
        out = _normalise_citations(["nope", 5, {"locator": "x"}, {"doc_id": None}], self.CHUNKS)
        assert out == []

    def test_non_list_input_is_safe(self):
        assert _normalise_citations("not-a-list", self.CHUNKS) == []
        assert _normalise_citations(None, self.CHUNKS) == []


class TestPII:
    def test_email_is_masked(self):
        assert mask_emails_in_text("mail me at sneha.k@example.com") == "mail me at s***@example.com"

    def test_phone_is_removed_entirely(self):
        out = strip_phones_in_text("Call +91 80 1234 5678 for help")
        assert "1234" not in out and "5678" not in out
        assert "Call" in out and "for help" in out

    def test_iso_dates_are_not_touched(self):
        assert strip_phones_in_text("effective 2026-12-01") == "effective 2026-12-01"

    def test_money_amounts_are_not_touched(self):
        assert strip_phones_in_text("band 34,00,000 per year") == "band 34,00,000 per year"

    def test_short_numbers_are_not_touched(self):
        assert strip_phones_in_text("3.5 days remaining") == "3.5 days remaining"


class TestProviderConfig:
    def test_defaults_are_current_production_models(self):
        from src.config import Settings
        expected = {
            "openai": "gpt-6-luna",
            "anthropic": "claude-haiku-4-5-20251001",
            "google": "gemini-3.5-flash-lite",
            "groq": "openai/gpt-oss-120b",
        }
        for provider, model in expected.items():
            assert Settings(llm_provider=provider, llm_model="").get_default_model() == model

    def test_explicit_model_overrides_default(self):
        from src.config import Settings
        s = Settings(llm_provider="groq", llm_model="my-model")
        assert s.get_default_model() == "my-model"

    def test_missing_key_raises_helpful_error(self):
        from src.config import Settings
        s = Settings(llm_provider="groq", groq_api_key="")
        with pytest.raises(ValueError, match="GROQ_API_KEY"):
            s.get_llm_client()

    def test_unknown_provider_is_rejected(self):
        from src.config import Settings
        with pytest.raises(ValueError, match="Unsupported LLM_PROVIDER"):
            Settings(llm_provider="banana").get_llm_client()

    def test_openai_compatible_requires_base_url(self):
        from src.config import Settings
        s = Settings(llm_provider="openai_compatible", llm_api_key="x", llm_base_url="")
        with pytest.raises(ValueError, match="LLM_BASE_URL"):
            s.get_llm_client()

    def test_openai_compatible_builds_client_with_custom_base_url(self):
        from src.config import Settings
        s = Settings(
            llm_provider="openai_compatible",
            llm_base_url="http://localhost:11434/v1",
            llm_api_key="dummy",
            llm_model="llama3.1:8b",
        )
        client = s.get_llm_client()
        assert "localhost:11434" in str(client.base_url)


class TestRateLimitHandling:
    """A transient provider 429 must not become a user-visible failure."""

    def test_detects_status_code_429(self):
        exc = Exception("boom")
        exc.status_code = 429
        assert _is_rate_limit_error(exc) is True

    def test_detects_rate_limit_text(self):
        assert _is_rate_limit_error(Exception("429 Too Many Requests")) is True
        assert _is_rate_limit_error(Exception("code: rate_limit_exceeded")) is True
        assert _is_rate_limit_error(Exception("invalid api key")) is False

    def test_reads_suggested_wait_from_message(self):
        exc = Exception("Rate limit reached ... Please try again in 15.8925s.")
        assert _rate_limit_wait_seconds(exc) == pytest.approx(15.8925)

    def test_reads_retry_after_header(self):
        class _Resp:
            headers = {"retry-after": "12"}

        exc = Exception("throttled")
        exc.response = _Resp()
        assert _rate_limit_wait_seconds(exc) == 12.0

    def test_returns_none_when_provider_gives_no_hint(self):
        assert _rate_limit_wait_seconds(Exception("throttled")) is None

    def test_call_llm_retries_once_on_throttling_then_succeeds(self, monkeypatch):
        from src.orchestration import router

        calls = {"n": 0}

        def fake_invoke(*_args, **_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception("429 rate limit reached")
            return '{"route": "no_answer"}'

        monkeypatch.setattr(router, "_invoke_provider", fake_invoke)
        monkeypatch.setattr(router.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(router.settings, "llm_rate_limit_retries", 1)
        monkeypatch.setattr(router.settings, "llm_rate_limit_max_sleep", 8.0)

        assert _call_llm("system", [], "rid") == '{"route": "no_answer"}'
        assert calls["n"] == 2

    def test_call_llm_does_not_retry_non_rate_limit_errors(self, monkeypatch):
        from src.orchestration import router

        calls = {"n": 0}

        def fake_invoke(*_args, **_kwargs):
            calls["n"] += 1
            raise Exception("invalid api key")

        monkeypatch.setattr(router, "_invoke_provider", fake_invoke)
        with pytest.raises(Exception, match="invalid api key"):
            _call_llm("system", [], "rid")
        assert calls["n"] == 1


class TestToolIntentDetection:
    """Leave-tool questions must still reach the model when retrieval finds nothing."""

    @pytest.mark.parametrize("message", [
        "How many annual leave days do I have left?",
        "Show my leave balance",
        "What is my remaining leave balance?",
        "Apply for annual leave from 2026-12-01 to 2026-12-03",
        "Please book some time off next week",
        "I need to take sick leave tomorrow",
        "Cancel my leave request",
        "Who on my team is on leave next week?",
    ])
    def test_recognises_tool_intents(self, message):
        from src.orchestration.router import _looks_like_leave_tool_request
        assert _looks_like_leave_tool_request(message) is True

    @pytest.mark.parametrize("message", [
        "How many paid sick days do I get each year?",
        "How many days of unused annual leave can I carry forward?",
        "What is the notice period for L4?",
        "What is the weather forecast in Paris?",
        "",
    ])
    def test_does_not_match_policy_questions(self, message):
        from src.orchestration.router import _looks_like_leave_tool_request
        assert _looks_like_leave_tool_request(message) is False


class TestDegradedFlag:
    """The API must distinguish 'provider unreachable' from 'model says no'."""

    def test_response_defaults_to_not_degraded(self):
        from src.main import ChatResponse

        r = ChatResponse(
            request_id="x", answer="a", route="no_answer",
            citations=[], tool_calls=[], pending_confirmation=False,
        )
        assert r.degraded is False

    def test_response_accepts_degraded_true(self):
        from src.main import ChatResponse

        r = ChatResponse(
            request_id="x", answer="a", route="no_answer",
            citations=[], tool_calls=[], pending_confirmation=False, degraded=True,
        )
        assert r.model_dump()["degraded"] is True

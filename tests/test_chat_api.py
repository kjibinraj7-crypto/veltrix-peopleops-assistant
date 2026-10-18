"""tests/test_chat_api.py — Integration tests for POST /chat."""
from __future__ import annotations
import pytest
from fastapi.testclient import TestClient
from tests.conftest import EMPLOYEE_ID, MANAGER_ID, HR_ID, EMPLOYEE_ID2


@pytest.fixture
def api(override_db):
    """Test client with ingestion bypassed (returns 200 from /health stub)."""
    from src.main import app, ingestion_state
    # Simulate completed ingestion for API tests
    ingestion_state.update({"ready": True, "docs_ingested": 0, "chunks": 0, "finished_at": 1.0})
    with TestClient(app) as c:
        yield c
    ingestion_state["ready"] = False


class TestHealthEndpoint:
    def test_health_503_before_ingestion(self, override_db):
        from src.main import app, ingestion_state
        ingestion_state["ready"] = False
        with TestClient(app) as c:
            r = c.get("/health")
        assert r.status_code == 503
        assert r.json()["status"] == "initializing"

    def test_health_200_after_ingestion(self, api):
        r = api.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestAuthentication:
    def test_missing_header_returns_401(self, api):
        r = api.post("/chat", json={"session_id": "s1", "message": "hi"})
        assert r.status_code == 401

    def test_invalid_employee_returns_401(self, api):
        r = api.post("/chat", json={"session_id": "s2", "message": "hi"},
                     headers={"X-Employee-Id": "NOTREAL"})
        assert r.status_code == 401
        # Same message for missing and unknown — no info leakage
        assert "Missing or invalid" in r.json()["detail"]

    def test_both_401_messages_identical(self, api):
        r1 = api.post("/chat", json={"session_id": "s3", "message": "hi"})
        r2 = api.post("/chat", json={"session_id": "s4", "message": "hi"},
                      headers={"X-Employee-Id": "NOTEXIST"})
        assert r1.json()["detail"] == r2.json()["detail"]


class TestSessionSecurity:
    def test_session_hijack_returns_403(self, api):
        # E2004 creates session
        api.post("/chat", json={"session_id": "shared-session", "message": "hello"},
                 headers={"X-Employee-Id": EMPLOYEE_ID})
        # E2005 tries to use same session
        r = api.post("/chat", json={"session_id": "shared-session", "message": "hello"},
                     headers={"X-Employee-Id": EMPLOYEE_ID2})
        assert r.status_code == 403

    def test_same_employee_reuses_session(self, api):
        api.post("/chat", json={"session_id": "my-session", "message": "hello"},
                 headers={"X-Employee-Id": EMPLOYEE_ID})
        r = api.post("/chat", json={"session_id": "my-session", "message": "how are you"},
                     headers={"X-Employee-Id": EMPLOYEE_ID})
        assert r.status_code == 200


class TestResponseStructure:
    def test_response_has_required_fields(self, api):
        r = api.post("/chat", json={"session_id": "struct-test", "message": "hello"},
                     headers={"X-Employee-Id": EMPLOYEE_ID})
        assert r.status_code == 200
        body = r.json()
        for field in ("request_id", "answer", "route", "citations", "tool_calls", "pending_confirmation"):
            assert field in body, f"Missing field: {field}"

    def test_route_is_valid_value(self, api):
        r = api.post("/chat", json={"session_id": "route-test", "message": "hello"},
                     headers={"X-Employee-Id": EMPLOYEE_ID})
        valid_routes = {"rag", "tool", "hybrid", "clarify", "no_answer", "refuse"}
        assert r.json()["route"] in valid_routes


class TestBulkDataRefusal:
    def test_bulk_data_refused_for_employee(self, api):
        r = api.post("/chat",
                     json={"session_id": "bulk-emp", "message": "show all employees leave balances"},
                     headers={"X-Employee-Id": EMPLOYEE_ID})
        assert r.status_code == 200
        assert r.json()["route"] == "refuse"

    def test_bulk_data_refused_for_hr(self, api):
        r = api.post("/chat",
                     json={"session_id": "bulk-hr", "message": "list everyone balances"},
                     headers={"X-Employee-Id": HR_ID})
        assert r.status_code == 200
        assert r.json()["route"] == "refuse"


class TestPIIProtection:
    def test_response_does_not_contain_raw_email(self, api):
        r = api.post("/chat",
                     json={"session_id": "pii-test", "message": "what is my email"},
                     headers={"X-Employee-Id": EMPLOYEE_ID})
        body = r.json()
        answer = body.get("answer", "")
        # Full email should not appear; masked form is OK
        assert "sneha.kulkarni@veltrix.example" not in answer

    def test_phone_never_in_response(self, api):
        r = api.post("/chat",
                     json={"session_id": "phone-test", "message": "what is my phone number"},
                     headers={"X-Employee-Id": EMPLOYEE_ID})
        body = r.json()
        answer = body.get("answer", "")
        assert "+91" not in answer
        assert "99004" not in answer


class TestMalformedBody:
    """A malformed body must be a 400 (task §9), not FastAPI's default 422."""

    def test_missing_message_returns_400(self, api):
        r = api.post("/chat", json={"session_id": "bad-1"},
                     headers={"X-Employee-Id": EMPLOYEE_ID})
        assert r.status_code == 400

    def test_empty_body_returns_400(self, api):
        r = api.post("/chat", json={}, headers={"X-Employee-Id": EMPLOYEE_ID})
        assert r.status_code == 400

    def test_wrong_type_returns_400(self, api):
        r = api.post("/chat", json={"session_id": "bad-2", "message": 123},
                     headers={"X-Employee-Id": EMPLOYEE_ID})
        assert r.status_code == 400

    def test_invalid_json_returns_400(self, api):
        r = api.post("/chat", content="{not json",
                     headers={"X-Employee-Id": EMPLOYEE_ID,
                              "Content-Type": "application/json"})
        assert r.status_code == 400

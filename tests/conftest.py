"""
tests/conftest.py
──────────────────
Shared pytest fixtures for all test modules.

Provides:
- test_db: A temporary SQLite database pre-seeded with the veltrix.db schema and
  sample data. Isolated per test — no shared state.
- test_client: An httpx AsyncClient pointed at the FastAPI app.
- sample_employees: Fixture returning Employee instances for common test roles.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from typing import Generator

# Tests seed their own DB and do not need a vector index — skip startup ingestion
# so the suite is fast and does not depend on faiss / the embedding model.
os.environ.setdefault("SKIP_INGESTION", "1")

import pytest
from fastapi.testclient import TestClient

# ─── Database fixtures ────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS employees (
    employee_id TEXT PRIMARY KEY,
    full_name   TEXT NOT NULL,
    email       TEXT NOT NULL,
    phone       TEXT NOT NULL,
    region      TEXT NOT NULL,
    join_date   TEXT NOT NULL,
    level       TEXT NOT NULL,
    role        TEXT NOT NULL,
    manager_id  TEXT REFERENCES employees(employee_id)
);

CREATE TABLE IF NOT EXISTS leave_balances (
    employee_id TEXT NOT NULL REFERENCES employees(employee_id),
    leave_type  TEXT NOT NULL,
    balance_days REAL NOT NULL,
    PRIMARY KEY (employee_id, leave_type)
);

CREATE TABLE IF NOT EXISTS leave_requests (
    request_id   TEXT PRIMARY KEY,
    employee_id  TEXT NOT NULL REFERENCES employees(employee_id),
    leave_type   TEXT NOT NULL,
    start_date   TEXT NOT NULL,
    end_date     TEXT NOT NULL,
    working_days INTEGER NOT NULL,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS holidays (
    region  TEXT NOT NULL,
    date    TEXT NOT NULL,
    name    TEXT NOT NULL,
    year    INTEGER NOT NULL,
    doc_id  TEXT NOT NULL,
    PRIMARY KEY (region, date)
);
"""

SEED_SQL = """
-- HR employees
INSERT INTO employees VALUES ('E1001','Meera Krishnan','meera.krishnan@veltrix.example','+91 98221 51875','IN','2016-07-11','L7','hr',NULL);
INSERT INTO employees VALUES ('E1002','Rohan Bhat','rohan.bhat@veltrix.example','+91 98614 77085','IN','2021-02-01','L4','hr','E1001');
INSERT INTO employees VALUES ('E1003','Dana Whitfield','dana.whitfield@veltrix.example','+1 512-555-0182','US','2019-09-16','L5','hr','E1001');
INSERT INTO employees VALUES ('E1004','Katrin Vogel','katrin.vogel@veltrix.example','+49 30 12345678','DE','2020-03-01','L5','hr','E1001');

-- IN managers
INSERT INTO employees VALUES ('E2001','Arjun Rao','arjun.rao@veltrix.example','+91 99001 11111','IN','2015-01-01','L7','manager',NULL);
INSERT INTO employees VALUES ('E2002','Divya Menon','divya.menon@veltrix.example','+91 99002 22222','IN','2017-05-10','L6','manager','E2001');
INSERT INTO employees VALUES ('E2003','Karthik Iyer','karthik.iyer@veltrix.example','+91 99003 33333','IN','2018-03-15','L5','manager','E2001');

-- IN employees
INSERT INTO employees VALUES ('E2004','Sneha Kulkarni','sneha.kulkarni@veltrix.example','+91 99004 44444','IN','2020-06-01','L3','employee','E2002');
INSERT INTO employees VALUES ('E2005','Vikram Shetty','vikram.shetty@veltrix.example','+91 99005 55555','IN','2021-01-15','L4','employee','E2002');

-- US manager + employee
INSERT INTO employees VALUES ('E3001','Michael Turner','michael.turner@veltrix.example','+1 512-555-0001','US','2014-04-01','L7','manager',NULL);
INSERT INTO employees VALUES ('E3004','Emily Carter','emily.carter@veltrix.example','+1 512-555-0004','US','2022-02-01','L3','employee','E3001');

-- DE manager + employee
INSERT INTO employees VALUES ('E4001','Stefan Wagner','stefan.wagner@veltrix.example','+49 30 99001111','DE','2015-09-01','L7','manager',NULL);
INSERT INTO employees VALUES ('E4004','Lena Hoffmann','lena.hoffmann@veltrix.example','+49 30 99004444','DE','2021-07-01','L3','employee','E4001');

-- Leave balances
INSERT INTO leave_balances VALUES ('E2004','annual',10.0);
INSERT INTO leave_balances VALUES ('E2004','sick',12.0);
INSERT INTO leave_balances VALUES ('E2005','annual',14.5);
INSERT INTO leave_balances VALUES ('E2005','sick',8.0);
INSERT INTO leave_balances VALUES ('E2001','annual',5.0);
INSERT INTO leave_balances VALUES ('E2001','sick',12.0);
INSERT INTO leave_balances VALUES ('E2002','annual',8.0);
INSERT INTO leave_balances VALUES ('E2002','sick',12.0);
INSERT INTO leave_balances VALUES ('E3004','annual',12.0);
INSERT INTO leave_balances VALUES ('E3004','sick',10.0);
INSERT INTO leave_balances VALUES ('E4004','annual',15.0);
INSERT INTO leave_balances VALUES ('E4004','sick',12.0);
INSERT INTO leave_balances VALUES ('E1001','annual',10.0);
INSERT INTO leave_balances VALUES ('E1001','sick',12.0);

-- Sample leave requests
INSERT INTO leave_requests VALUES (
    'req-001','E2004','annual','2026-02-16','2026-02-20',5,'approved','2026-01-10T13:23:00'
);
INSERT INTO leave_requests VALUES (
    'req-002','E2004','sick','2026-05-04','2026-05-05',2,'approved','2026-05-04T17:35:00'
);
INSERT INTO leave_requests VALUES (
    'req-pending-001','E2004','annual','2026-11-09','2026-11-13',4,'pending','2026-10-01T17:56:00'
);

-- Sample holidays (IN region)
INSERT INTO holidays VALUES ('IN','2026-01-26','Republic Day',2026,'CAL-HOL-IN-2026');
INSERT INTO holidays VALUES ('IN','2026-08-15','Independence Day',2026,'CAL-HOL-IN-2026');
INSERT INTO holidays VALUES ('IN','2026-10-02','Gandhi Jayanti',2026,'CAL-HOL-IN-2026');
INSERT INTO holidays VALUES ('US','2026-07-04','Independence Day',2026,'CAL-HOL-US-2026');
INSERT INTO holidays VALUES ('DE','2026-10-03','German Unity Day',2026,'CAL-HOL-DE-2026');
"""


@pytest.fixture(scope="function")
def test_db_path(tmp_path) -> Generator[str, None, None]:
    """
    Create a fresh temporary SQLite database for each test function.
    Pre-seeded with schema + representative sample data.
    """
    db_file = str(tmp_path / "test_veltrix.db")
    conn = sqlite3.connect(db_file)
    conn.executescript(SCHEMA_SQL)
    conn.executescript(SEED_SQL)
    conn.commit()
    conn.close()
    yield db_file
    # tmp_path is automatically cleaned up by pytest


@pytest.fixture(scope="function", autouse=False)
def override_db(test_db_path, monkeypatch):
    """
    Override the DB path in settings so all data_access calls use the test DB.
    """
    import src.data_access.db as db_module

    # Close any existing connection from previous tests
    db_module.close_connection()

    # Patch the DB path in settings
    monkeypatch.setenv("DB_PATH", test_db_path)

    # Invalidate the settings cache so DB_PATH is re-read
    from src.config import get_settings
    get_settings.cache_clear()

    yield

    # Cleanup: close test connection
    db_module.close_connection()
    get_settings.cache_clear()


# ─── FastAPI test client ──────────────────────────────────────────────────────

@pytest.fixture(scope="function")
def client(override_db) -> Generator[TestClient, None, None]:
    """
    Synchronous test client for the FastAPI app.
    Uses the test database via override_db fixture.
    """
    from src.main import app
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ─── Common employee IDs ──────────────────────────────────────────────────────

# Use these constants in tests for clarity and easy grep-ability.
EMPLOYEE_ID   = "E2004"   # Sneha Kulkarni — regular employee (IN)
EMPLOYEE_ID2  = "E2005"   # Vikram Shetty — another employee under E2002
MANAGER_ID    = "E2002"   # Divya Menon — manager of E2004 and E2005
MANAGER_ID2   = "E2001"   # Arjun Rao — top-level manager IN
HR_ID         = "E1001"   # Meera Krishnan — head HR
US_EMPLOYEE_ID = "E3004"  # Emily Carter — US employee
DE_EMPLOYEE_ID = "E4004"  # Lena Hoffmann — DE employee

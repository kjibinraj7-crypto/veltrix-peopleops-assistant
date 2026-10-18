#!/usr/bin/env python
"""
scripts/eval_server.py
──────────────────────
Run the API against a THROWAWAY copy of the SQLite database.

Why: the evaluation set includes write-path questions (leave requests). Pointing
the API at a copy means a review run can never mutate `data/veltrix.db`.

The corpus and vector store are used as-is, so startup reuses the existing index
when the corpus is unchanged.

Usage
-----
    python scripts/eval_server.py                 # http://127.0.0.1:8100
    python scripts/eval_server.py --port 8100

    # then, in another shell:
    python eval/run_eval.py --base-url http://127.0.0.1:8100

Requires DB_PATH to be overridable (it is — see src/config.py) and an LLM key in
`.env` (loaded automatically by src.config).
"""
from __future__ import annotations

import argparse
import os
import shutil
import tempfile


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the API against a throwaway DB copy.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--db", default=os.path.join("data", "veltrix.db"))
    args = ap.parse_args()

    workdir = tempfile.mkdtemp(prefix="veltrix-eval-db-")
    db_copy = os.path.join(workdir, "veltrix.db")
    if os.path.isfile(args.db):
        shutil.copy2(args.db, db_copy)
        print(f"Copied {args.db} -> {db_copy}")
    else:
        print(f"WARNING: {args.db} not found; the API will create an empty database.")

    # Must be set before src.config is imported.
    os.environ["DB_PATH"] = db_copy
    print(f"DB_PATH = {db_copy}")

    import uvicorn  # imported late so DB_PATH is already set

    print(f"Starting API on http://{args.host}:{args.port}")
    uvicorn.run("src.main:app", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

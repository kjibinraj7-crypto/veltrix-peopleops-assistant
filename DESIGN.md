# DESIGN.md — Veltrix PeopleOps Assistant

Two-page design note. Access-control code paths and failure modes are named
explicitly so they can be audited.

## 1. Ingestion (`src/ingestion/`)

Runs at startup, before `/health` returns 200 (`run_ingestion` in
`ingestor.py`). A SHA-256 over the corpus directory plus manifest
(`corpus_hash.py`) decides between reusing the saved FAISS index and rebuilding.

| Format | Parser | Notes |
|---|---|---|
| PDF | `parsers/pdf_parser.py` (PyMuPDF) | Text layer used when present; any page that looks scanned **or** yields `< MIN_PAGE_CHARS` text is rasterised and re-read by RapidOCR (pip-only ONNX, no system binaries) |
| DOCX | `parsers/docx_parser.py` | `doc.element.body` order preserves paragraph/table interleaving |
| HTML | `parsers/html_parser.py` (BS4 + lxml) | Scripts/styles stripped, tables converted to markdown |
| MD | `parsers/markdown_parser.py` | Split on `#` headings |

Tables are parsed **generically**: header cells are matched by keyword
(`date`, `holiday`, `occasion`, …), never by column index, so a re-ordered
calendar still parses. `chunker.py` splits section-aware into ~300–500-token
chunks with 50-token overlap, each carrying `doc_id`, `doc_type`, `regions`,
`effective_date`, `access_level`, `section_heading`, `page_number`.
`holiday_extractor.py` parses calendar documents into the `holidays` table,
keyed `(region, date)`, for working-day maths. Embeddings are
`BAAI/bge-small-en-v1.5` (384-dim, CPU); queries get the BGE prefix
(`embedder.py`), documents do not.

**Integrity checks (`integrity.py`).** No page may disappear silently. Every page
is classified after extraction (`classify_page`) and every document after
chunking (`classify_document`):

| Check | Rule | Outcome |
|---|---|---|
| Extraction | chars recovered by the parser | input to the page checks |
| OCR attempt | page looks scanned **or** has `< MIN_PAGE_CHARS` | page re-read with OCR |
| Post-OCR | still `< MIN_PAGE_CHARS` | **FLAG** `flagged_thin_text` / `flagged_empty` |
| Document | `< MIN_DOCUMENT_CHARS` or 0 chunks | **FLAG** |
| Manifest | file in `manifest.json` is missing, or the parser raised | **FLAG** |

The report is printed **before embeddings are built**, written to
`vector_store/ingestion_report.json` stamped with the corpus hash (reused on an
unchanged corpus, regenerated when it changes), and summarised on `/health`.

## 2. Retrieval (`src/retrieval/`)

```
Query → FAISS + BM25 → RRF (k=60) → top-30          (hybrid_retriever.retrieve)
      → access-control filter  ← MANDATORY PRE-LLM   (access_filter.filter_chunks)
      → confidence threshold (<0.30 → no_answer)     (is_below_confidence_threshold)
      → rerank                                       (reranker.rerank, flashrank L-12)
      → precedence sort                              (precedence.sort_chunks_by_precedence)
      → token budget ≤3000                           (token_budget.select_chunks_within_budget)
      → <DOC id="…"> context → LLM
```

Document text is escaped for `<DOC`/`</DOC>` before it is placed in the prompt, so
a document cannot close its own block and address the model as if outside the
reference-data region. The system prompt still instructs the model to treat
everything inside `<DOC>` as data and never as instructions (`new_joiner_tips.html`
in the corpus is exactly such a trap); escaping is the structural half of that
defence.

RRF (`hybrid_retriever._rrf_merge`) fuses inner-product cosine FAISS results with
BM25 Okapi, so exact tokens ("India", "GLOBAL") are not lost. The reranker is a
cross-encoder (`ms-marco-MiniLM-L-12-v2`) and falls back to RRF order if
flashrank is missing. Token counting always uses `tiktoken cl100k_base`
regardless of provider, and the count of every LLM call is logged.

**Precedence** (`precedence.py`): (1) authority — `policy|addendum|memo|handbook|calendar`
are one tier, above `faq`, above `wiki`; (2) scope — region-specific beats
`GLOBAL`; (3) recency — later `effective_date` wins. Documents whose
`effective_date` is in the future are held in a separate bucket and only
mentioned as an upcoming change.

## 3. Access Control (`src/security/`)

| Concern | Enforced in | When |
|---|---|---|
| Document `access_level` vs role | `access_control.can_role_read_doc`, called by `retrieval/access_filter.filter_chunks` | **Before** any chunk is put in the prompt |
| Region scoping (requester region or `GLOBAL`) | `access_filter.filter_chunks` | Before the prompt |
| Own-leave RBAC | `access_control.can_access_leave_data`, called inside every tool (`tools/*.py`) | At tool execution |
| Tool permissions | `access_control.can_use_tool` | At tool execution |
| Bulk-data refusal | `access_control.is_bulk_data_request` | First step of the router |
| Identity (never from message text) | `security/identity.resolve_identity` (`X-Employee-Id` → DB) | FastAPI dependency |

Restricted chunks are dropped silently — never named or hinted at. An
unauthorised leave-data lookup returns one fixed refusal
(`access_control.LEAVE_DATA_REFUSAL`) whether or not the employee exists, so the
response cannot be used as an existence oracle. Role→rights: employee = `all`
docs, self only; manager = `all`+`managers` docs, self + **direct** reports;
hr = all docs, any single employee but never bulk.

## 4. Semantic cache (`src/orchestration/cache.py`)

Sits **before retrieval**, so a hit skips retrieval, reranking and the LLM call.

```
Query → embed → cosine vs stored queries in the same scope
      → best ≥ SEMANTIC_CACHE_THRESHOLD (0.93) and same corpus fingerprint?
      → yes: return stored answer (cost 0, latency ≈ embedding)
```

Three invariants stop this from becoming a correctness or privacy hazard:

1. **Corpus-bound.** Each entry records the corpus fingerprint it was produced
   from. On startup the fingerprint is set from the ingestion hash; an entry
   whose fingerprint differs is a miss and is pruned. *Documents updated ⇒
   old answers are never reused.* A process that has never ingested (no
   fingerprint) stores nothing at all.
2. **Requester-scoped.** The key is `(employee_id, role, region)`. Repeating a
   question is served from cache; a different employee asking the identical
   question is not — caching can never leak one person's answer to another.
3. **Route-restricted.** Only grounded `rag` answers are stored; `tool`/`hybrid`
   answers contain live leave data, and `no_answer` is a negative result that is
   not entrenched for the whole TTL.

Storage is local (`vector_store/semantic_cache.json`), flushed periodically and
at shutdown, so it survives restarts. `/health` exposes entries/hits/stores.

## 5. Orchestration & API contract (`src/orchestration/router.py`, `src/main.py`)

`handle_message` runs the pipeline and returns the six-field response. After the
model replies, two guards run: `_normalise_citations` keeps only citations whose
`doc_id` was retrieved **and** is eligible for this requester (locator filled
from chunk metadata when missing), and `_enforce_route_contract` forces the
`route`/`citations`/`tool_calls` combination to legal shapes — `rag` needs a
citation, `tool` needs a tool call, `hybrid` needs both, and
`clarify`/`no_answer`/`refuse` carry neither. Write tools
(`apply_leave`, `cancel_leave`) are validated without touching the database and
stored as a session proposal; a second confirming message executes them
atomically. A session holds at most one proposal, and any other message discards
it. `/chat` enforces a hard per-request ceiling below the 30-second limit.

## 6. Failure Modes — found, and what was done

| Failure found | Fix |
|---|---|
| Reranker model ID was invalid, so flashrank 404'd and was silently swallowed — reranking never ran | Corrected to `ms-marco-MiniLM-L-12-v2`; loading now verified |
| Corpus hash used file size + mtime, so a same-size edit could be missed | Hashing includes file contents |
| Malformed request bodies returned FastAPI's default `422`, not the specified `400` | `RequestValidationError` handler returns 400 |
| A cancelled proposal returned `route:"tool"` with an empty tool list, violating the contract | Returns `clarify`; the contract is enforced server-side for every path |
| Provider defaults pointed at retired models / an EOL Google SDK | Current models per provider, `google-genai` client, plus `openai_compatible` + `LLM_BASE_URL` |
| The retrieval-confidence gate returned before the LLM, so leave-tool questions never reached the tools | Tool-intent patterns bypass the gate and an explicit "no documents retrieved" note stops general-knowledge fallback |
| A semantic cache that ignored the requester would have leaked one employee's answer to another | Cache scope is `(employee_id, role, region)`; corpus fingerprint enforces invalidation |
| `_maybe_save_locked()` called `save()` under a non-reentrant lock → deadlock on first cache write | Lock is reentrant (`threading.RLock`) |
| Cached answers from an older corpus could be replayed whenever ingestion did not run (e.g. `SKIP_INGESTION`), because the fingerprint read from the cache file was trusted | The on-disk fingerprint is never adopted: only the ingestion pipeline may declare the served corpus, so without it no entry is usable |
| Caching a `no_answer` would freeze one retrieval/model failure for the whole TTL | Only grounded `rag` answers are cached; `no_answer` is never reused |
| The cache embedded the query on the event loop, blocking the per-request timeout | Cache `get`/`set` run in a worker thread (`loop.run_in_executor`) |
| Document text containing `</DOC>` could close its own reference-data block and address the model as if outside it | `<DOC`/`</DOC>` are escaped in chunk text before the prompt is built (`token_budget._neutralise_doc_tags`) |
| A manifest entry could name a file outside `./data/corpus` (`../…`, absolute path) and have it parsed | Every resolved path must stay inside the corpus root, otherwise the entry is reported as a missing file (`ingestor._is_within`) |
| The session store grew without bound — `cleanup_stale` existed but was never called | Idle sessions expire opportunistically on request (never under the store lock) |

Remaining graceful degradations: reranker unavailable → RRF order; BM25
unavailable → dense only; OCR fails on a page → page **flagged** in the
integrity report (not silently skipped); holiday table empty → Mon–Fri only;
LLM error or timeout → HTTP 200 with `route:"no_answer"`, `degraded:true` and a
retry message (never a crash or a 5xx).

## 7. Scale & Limitations

In-memory sessions (single pod); single-file SQLite in WAL mode (concurrent
reads, serialised writes); FAISS flat index suits 30–300 documents, HNSW would
be needed at 10k+ chunks; leave year fixed to 2026 per the specification.

# Features — required by the task, and what we added

This document separates three things that are easy to blur together:

1. **Required** — behaviour the task brief asks for.
2. **Fixed** — scaffolding that already existed but was broken; repaired so the required
   behaviour actually works.
3. **Added** — features we chose to build on top, grouped by the property they buy:
   **cost**, **speed**, **accuracy**, **security**, **robustness**.

Everything in sections 2–7 is an addition. They are listed separately and each names the
file that implements it, so a reviewer can accept or reject any of them individually
without disturbing the required behaviour.

Companion documents: `README.md` (run it), `WORKFLOW.md` (how a request flows),
`DESIGN.md` (trade-offs and failure analysis).

---

## 1. Required by the task

| Requirement | Where it lives | Status |
|---|---|---|
| HTTP service with `GET /health` (200 within 10 min) and `POST /chat` | `src/main.py` | ✅ cold start → 200 in **41 s** |
| `docker compose up` with zero manual steps (models pre-baked, no apt-get) | `Dockerfile`, `docker-compose.yml` | ✅ no runtime downloads |
| RAG corpus Q&A **with citations** | `src/retrieval/*`, `src/orchestration/router.py` | ✅ citation must come from a retrieved + eligible chunk |
| Precedence when documents conflict | `src/retrieval/precedence.py` | ✅ authority → scope → recency |
| Region-specific rules applied per requester | `src/retrieval/access_filter.py` | ✅ IN / US / DE / GLOBAL |
| ≤ **3,000 tokens** of corpus per LLM call, counted with `tiktoken cl100k_base` | `src/retrieval/token_budget.py` | ✅ counts the `<DOC>` wrapper too |
| 5 leave tools (read + write) | `src/tools/` | ✅ `get_leave_balance`, `list_leave_requests`, `get_team_leave`, `apply_leave`, `cancel_leave` |
| Second confirmation before any write | `src/orchestration/confirmation.py`, `router.py` | ✅ cannot be bypassed by message content |
| Role-based access control for documents **and** leave data | `src/security/access_control.py` | ✅ pre-LLM filter + per-tool authorisation |
| PII protection in prompts, responses and logs | `src/security/pii_filter.py` | ✅ emails masked, phones removed, logs keyed by employee id |
| Multi-turn conversations | `src/orchestration/session.py` | ✅ history window of 10 turns |
| Refuse irrelevant / out-of-scope questions | `src/orchestration/prompts.py` | ✅ `route: refuse` |
| Prompt-injection resistance (the corpus contains a trap) | `prompts.py`, `token_budget.py` | ✅ refused (see `WORKFLOW.md` example 5) |
| Bulk-data extraction refused; no existence oracle | `access_control.py`, `identity.py` | ✅ one fixed refusal for both cases |
| Session hijack → 403, unknown employee → 401 | `session.py`, `identity.py` | ✅ |
| OCR for scanned documents | `src/ingestion/parsers/pdf_parser.py` | ✅ pip-only (PyMuPDF + RapidOCR) |
| Works with a **modified corpus** — zero hardcoded facts | `data/`, `eval/dataset/` | ✅ no corpus fact exists in `src/` |
| Works with other LLM APIs | `src/config.py` | ✅ openai / anthropic / google / groq / `openai_compatible` |
| ≤ 30 s per request | `src/main.py` | ✅ 25 s hard ceiling; observed max 13.3 s |
| 20-question evaluation with a graded answer sheet | `eval/` | ✅ 20/20 auto, **19/20 (95 %)** after manual review |

---

## 2. Fixed — the scaffolding was not wrong, just broken

Each of these silently produced wrong output until repaired; several are why specific
evaluation questions now pass.

| Defect | Consequence before the fix | Fix |
|---|---|---|
| HTML parser crashed on every HTML file | FAQ / V2-summary / wiki / new-joiner / US-calendar content lost entirely | `ingestion/parsers/html_parser.py` |
| PDF calendars never yielded holidays | India holidays missing → working-day counts wrong | `ingestion/holiday_extractor.py` uses `page.find_tables()` |
| Holiday extractor mistook metadata tables for holiday tables | Wrong holiday names/dates | per-table header detection; restricted tables skipped |
| Reranker model ID was invalid → 404 swallowed | cross-encoder never ran; retrieval silently un-reranked | corrected to `ms-marco-MiniLM-L-12-v2`, load verified |
| Corpus hash was size+mtime | a same-size edit went undetected | hashes file **contents** |
| Write actions could execute from the model's own `status` | a model claim could cause a real database write | writes always go through propose → confirm |
| Write proposals were unvalidated | user could confirm an impossible action | validated at propose time with a specific reason |
| Token budget ignored the `<DOC>` wrapper | injected corpus text could exceed 3,000 tokens | budget counts formatted blocks + overhead |
| `pending_confirmation` echoed the model's flag | response could claim a write that never happened | derived from session state |
| Malformed body returned `422`, not `400` | contract violation | `RequestValidationError` handler |
| Container healthcheck used `curl` (absent in slim) | container reported unhealthy | Python `urllib` probe |
| Provider defaults pointed at retired models | every call would fail at evaluation time | current models + `openai_compatible` escape hatch |
| OCR needed `apt-get`/Poppler/Tesseract | heavier image, external binaries | PyMuPDF + RapidOCR, pip-only |
| Default `torch` wheel pulled ~2 GB CUDA | slow, bloated build | CPU-only torch index |

---

## 3. Added — cost effectiveness

| Feature | What it does | Where | Measured / expected effect |
|---|---|---|---|
| **Semantic answer cache** | Reuses an answer when a new question means the same thing, instead of re-retrieving and re-calling the LLM. Local JSON store, survives restarts. | `src/orchestration/cache.py` | repeat question **13,336 ms → 238 ms**, **0 tokens** |
| Cache scoped by `(employee_id, role, region)` | Prevents cross-user reuse and makes the cache safe to enable | `cache.py` | same question, employee vs HR → different (correct) answers |
| Cache bound to a **corpus fingerprint** | Document updates invalidate every affected answer automatically | `cache.py`, `ingestor.py` | corpus change → all old entries pruned |
| Cache restricted to grounded `rag` answers | `tool`/`hybrid` carry live leave data; `no_answer` is a negative result | `cache.py` | a failure cannot be frozen for 7 days |
| Bounded cache + TTL | 500 entries, 7 days; lowest-hit/oldest entries evicted first | `cache.py` | memory cannot grow without bound |
| Hash-gated ingestion | Skips re-parsing/re-embedding when the corpus is unchanged | `ingestion/corpus_hash.py` | startup **37 s → 4.3 s** |
| Reranker + budget interaction | Only the most relevant chunks are sent, so the 3,000-token allowance is spent on useful text | `retrieval/*` | fewer retries, shorter prompts |
| Confidence gate | Skips the LLM entirely when retrieval found nothing usable | `router.py` | no tokens spent on unanswerable questions |
| Deterministic guards before the LLM | Bulk-data and out-of-scope requests are refused without a model call | `security/access_control.py` | bulk refusal in **21 ms**, 0 tokens |
| Temperature 0 | Consistent answers across runs, fewer re-runs during validation | `config.py` | reproducible scoring |
| One-line provider switch | Avoids re-architecting to use a cheaper/different model | `config.py` | `LLM_PROVIDER` + key |

---

## 4. Added — speed

| Feature | Where | Effect |
|---|---|---|
| **Hybrid retrieval in one pass** — FAISS dense + BM25 sparse fused with RRF (k=60) | `retrieval/hybrid_retriever.py` | both retrievers run once, in-process — no extra round-trip and no second model call |
| Reranking limited to top-30 candidates | `retrieval/reranker.py`, `config.py` | cross-encoder cost bounded |
| Models baked into the image (BGE, RapidOCR, flashrank) | `Dockerfile` | no first-request download |
| Startup completes ingestion before serving | `main.py` | first request is never a cold start |
| Cache hit path skips retrieval + LLM | `cache.py`, `router.py` | **238 ms** end-to-end |
| Blocking work off the event loop | `router.py`, `main.py` | embedding/cache I/O in a worker thread; the 25 s ceiling can actually fire |
| 25 s hard ceiling → degraded answer | `main.py` | a slow provider cannot hold a connection past the SLA |
| Session cleanup on request | `session.py` | session store cannot degrade over a long run |
| Local, offline OCR | `parsers/pdf_parser.py` | no network, no external service on the hot path |

---

## 5. Added — accuracy

| Feature | Why it improves answers | Where |
|---|---|---|
| **Hybrid retrieval (dense + sparse)** — BM25 surfaces keyword-exact chunks the dense index can miss (policy titles, section numbers, holiday names, acronyms), then RRF fuses the two rankings | Measured against the saved index: BM25 contributed **4 / 5 / 9** candidates to the fused top-30 across three test queries that dense-only retrieval never surfaced | `retrieval/hybrid_retriever.py` |
| **Precedence engine** (authority → scope → recency) with an explicit tie-break that surfaces both values | The corpus deliberately contradicts itself (`V1` vs `V2` vs FAQ vs wiki vs memo); without this a model can average or pick the wrong source | `retrieval/precedence.py` |
| Future-dated documents separated into an "upcoming change" note | `POL-LEAVE-GLOBAL-V3` (effective 2027-01-01) must not be applied today | `precedence.py` |
| Region scoping as a first-class filter | Berlin must not inherit a Bengaluru memo | `retrieval/access_filter.py` |
| Cross-encoder reranking | Moves the genuinely relevant section above keyword-noise | `retrieval/reranker.py` |
| BGE asymmetric query prefix, applied to queries only | Measured retrieval-quality regression guard | `ingestion/embedder.py`, `tests/test_embedder.py` |
| Section-aware chunking + full metadata per chunk | Makes citations precise (`DOC#section`) instead of page-level guesses | `ingestion/chunker.py` |
| Holidays extracted into SQLite | Working-day answers are computed from real calendar rows, not model arithmetic | `ingestion/holiday_extractor.py`, `utils/working_days.py` |
| Restricted/optional holidays excluded | The office is open, so they must not reduce a working-day count | `holiday_extractor.py` |
| Tool-intent bypass at the confidence gate | Personal leave questions reach the tools instead of being rejected as "no documents" | `router.py` |
| Explicit "no documents retrieved" prompt note | Stops the model answering from general knowledge when the corpus is silent | `router.py` |
| Route-contract enforcement | Route/citation/tool combinations are forced into one of six legal shapes | `router.py` |
| Citation validation against retrieved **and** eligible chunks | The model cannot cite a document it did not receive, or one the requester may not see | `router.py` |
| Typographic normalisation in the grader | A correct answer is not marked wrong for using a narrow no-break space or a non-breaking hyphen | `eval/run_eval.py` |
| Data-driven evaluation set | Questions, expected answers and grading rules are data, so a modified corpus needs a new dataset — **not** new code | `eval/dataset/qa_dataset.json` |

**Measured:** 20/20 auto-graded, **19/20 (95 %)** after reading every answer, 0 errors,
0 retries, 0 degraded answers. The single failure is documented in `README.md` §Known
Limitations and in the workbook's `reviewer_notes`.

**Hybrid-search caveats (stated deliberately, not hidden):**

- The confidence gate uses the **dense** similarity, not the fused score. `retrieve()`
  returns `best_dense_score`, and `is_below_confidence_threshold` compares it to `0.30`.
  RRF scores live on a different scale (a chunk ranked first by only one retriever
  scores ≈0.016; the maximum, first in both, is ≈0.033) — far below the 0.30 cosine
  threshold, so they cannot be compared against it. Consequence: BM25 changes candidate
  **ranking and recall**, not the "is anything relevant?" decision.
- BM25 tokenisation is a plain `query.lower().split()` with `rank_bm25` defaults — no
  stemming and no stop-word list. Adequate at 114 chunks; it would need work on a much
  larger corpus.
- If the BM25 index is missing or fails to load, retrieval silently degrades to
  dense-only (a warning is logged, the request still succeeds). This is the fallback
  listed in §7, and it is the one way to run the pipeline *without* hybrid retrieval.

---

## 6. Added — security and privacy

| Addition | Threat it closes | Where |
|---|---|---|
| Cache scoped per requester **and** role/region | Cross-employee or cross-role disclosure via the cache | `cache.py` |
| Cache fingerprint never trusted from the file | Serving stale policy answers after an update (or when ingestion is skipped) | `cache.py` |
| Only `rag` answers cached | Freezing a retrieval failure or serving live leave data from cache | `cache.py` |
| `<DOC>` / `</DOC>` escaped inside document text | A document closing the reference block and speaking as if outside it | `token_budget.py` |
| Manifest paths confined to the corpus root | A manifest entry (`../…`, absolute path) being parsed as a document | `ingestor._is_within` |
| PII scrub **before** caching and before returning | Cached answers never hold raw emails/phones | `router.py`, `pii_filter.py` |
| Identical refusal for unknown vs unauthorised | Existence oracle on employees and on other people's requests | `access_control.py`, `identity.py` |
| Bulk guard fires for every role, and discards pending writes | Privilege escalation via enumeration; a write piggybacking a refusal | `router.py` |
| Identity only from `X-Employee-Id` → DB | Message text claiming a different role/manager is ignored | `identity.py`, `prompts.py` |
| Session bound to its owner → 403 | Session hijacking / history theft | `session.py` |
| Writes cannot execute from model output alone | A model-injected "success" status causing a real database write | `router.py`, `confirmation.py` |
| Tool names allow-listed | Model-supplied tool names cannot reach arbitrary code | `tools/registry.py` |
| Parameterised SQL everywhere | SQL injection | `data_access/*_repo.py` |
| `.env` excluded from git **and** the Docker build context | Credential leakage into the image or the repository | `.gitignore`, `.dockerignore` |
| CORS without `allow_credentials` | Credentialed cross-origin abuse of a local service | `main.py` |
| API keys whitespace-stripped | A pasted `KEY= value` producing a malformed Authorization header | `config.py` |
| Session expiry | Unbounded memory growth from invented session ids | `session.py` |
| No HTML rendering anywhere | XSS (answers are JSON; ingested HTML is reduced to text) | `main.py`, `html_parser.py` |

---

## 7. Added — robustness, observability, fallbacks

| Addition | Purpose | Where |
|---|---|---|
| **Ingestion integrity report** — per-page char counts, OCR usage, flagging for thin/empty pages, document-level checks, missing files, parse errors | Makes "did we lose a document?" answerable; a doubt is **printed**, never swallowed | `ingestion/integrity.py` |
| OCR recovery for thin pages | A scanned memo still contributes text | `parsers/pdf_parser.py` |
| Report persisted and stamped with the corpus hash | A stale report is never reused; `/health` always reports the truth | `integrity.py`, `ingestor.py` |
| `degraded: true` on unreachable provider **or** non-JSON reply | A harness can retry instead of scoring infrastructure failure as inaccuracy | `router.py` |
| Bounded, capped retry on 429 (≤ 8 s) | Survives transient throttling **without** breaking the 30 s rule | `router.py` |
| Harness-side pacing and retries with fresh session ids | Keeps a throttled run from polluting history or being scored as wrong | `eval/run_eval.py` |
| Graceful degradation everywhere (see table below) | No single optional dependency can take the service down | various |
| Structured JSON logging with `request_id`, `route`, `latency_ms` | Debuggable without exposing personal data | `utils/logging.py` |
| Enriched `/health` (docs, chunks, integrity, cache stats, fingerprint) | One endpoint to see the true state of the service | `main.py` |
| Every optional stage reports its own health | Failures are visible rather than silent | `/health`, logs |

### Fallback matrix

| If this fails | The service still… | Where |
|---|---|---|
| Reranker model unavailable | retrieves in RRF order | `reranker.py` |
| BM25 unavailable | retrieves densely only | `hybrid_retriever.py` |
| OCR fails on a page | flags the page in the integrity report | `pdf_parser.py` |
| Holiday table empty | counts Mon–Fri only | `working_days.py` |
| LLM unreachable / times out | returns `200 no_answer`, `degraded: true` | `router.py` |
| Model returns non-JSON | returns `200 no_answer`, `degraded: true` | `router.py` |
| Request exceeds 25 s | returns a degraded answer, inside the SLA | `main.py` |
| Cache file corrupt | discards it and starts empty | `cache.py` |
| Unhandled exception | returns `503` JSON, never a crash | `main.py` |

---

## 8. Added — evaluation tooling

| Addition | Why |
|---|---|
| `eval/dataset/qa_dataset.json` — questions, expected answers, grading rules as **data** | Satisfies "no hardcoded document content": a modified corpus gets a new dataset, not new code |
| `eval/run_eval.py` — generic runner | Reads the dataset, calls `/chat`, records the answer, auto-grades, writes Excel, prints the rate |
| Auto-grading rules (required terms, forbidden terms, regex, any-of) | Coarse but reproducible first pass |
| `reviewer_verdict` / `reviewer_notes` columns + a `Summary` sheet with both rates | Human review is recorded next to the machine score, so the honest figure is visible |
| `ERROR` is distinct from `FAIL` | An outage can never be reported as inaccuracy |
| Fresh session id per retry | A failed turn cannot pollute the successful one |
| `--delay` / `--retries` / `--retry-wait` pacing | Provider rate limits are handled **outside** the request, keeping the 30 s rule intact |
| Reference date pinned in the dataset meta | Date-sensitive questions stay reproducible |

---

## 9. Cost, speed and accuracy — measured on this machine

| Metric | Value |
|---|---|
| Cold start, `vector_store/` deleted → `/health` 200 | **41 s** (ingestion 37.2 s) |
| Restart with unchanged corpus → `/health` 200 | **4.3 s** |
| Typical document question (retrieval + rerank + LLM) | **3.7 – 5.4 s** |
| Repeat question (semantic cache hit) | **238 ms**, 0 tokens |
| Deterministic refusal (bulk / out-of-scope) | **21 ms**, 0 tokens |
| Worst observed request | **13.3 s** (first call of a run) — inside the 30 s rule |
| Corpus sent per LLM call | ≤ 3,000 tokens, enforced |
| Ingestion integrity | 17 pages, 1 OCR-recovered, **0 flagged**, 30/30 docs, 0 parse errors |
| Evaluation | 20/20 auto-graded; **19/20 (95 %)** manual; 0 errors, 0 retries, 0 degraded |
| Tests | **178 passing** |

---

## 10. What we deliberately did **not** add

Scope discipline matters as much as the feature list. These were considered and rejected:

| Not added | Why not |
|---|---|
| A sixth "working days calculator" tool | The brief specifies five tools; the needed behaviour belongs in retrieval/prompting, not in a new tool surface. |
| Multi-tenant / external vector database | 30 documents and 114 chunks fit comfortably in an in-process FAISS index; an external service would add a network hop and a failure mode for no benefit at this size. |
| Agentic loops / self-critique passes | Each extra LLM round-trip multiplies cost and latency, and the 30 s ceiling leaves little room; single-pass with good retrieval scored 95 %. |
| Semantic cache for `tool`/`hybrid` answers | Leave balances change with every write; caching them would return stale personal data. |
| Caching `no_answer` | It would entrench a retrieval or model failure for the full TTL. |
| Tuning retrieval to force question 12 to pass | It is one honest retrieval miss; a targeted change risked regressing the 14 questions that already pass. It is documented instead. |
| Storing the API key anywhere but `.env` | The key stays a deployment secret; `.env.example` is the committed template. |

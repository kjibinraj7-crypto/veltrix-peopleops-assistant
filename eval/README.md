# Evaluation harness

Measures answer quality/accuracy of the Veltrix PeopleOps Assistant against a
fixed question set derived from `data/corpus/`.

## Design principle — no hardcoded document content

All facts, expected answers and grading rules live in **data files**, never in code:

- `eval/dataset/qa_dataset.json` — 20 questions + expected answers + grading rules.
- `eval/run_eval.py` — **generic** runner: reads the dataset, calls `POST /chat`,
  records the actual answer, auto-grades it, writes Excel, prints a success rate.

To evaluate a **modified corpus**, write a new dataset JSON and pass
`--dataset <path>`; **the code does not change**.

## Files

| File | Purpose |
|---|---|
| `dataset/qa_dataset.json` | Questions, expected answers, grading rules (data only) |
| `run_eval.py` | Generic runner + auto-grader + Excel writer |
| `results/eval_results.xlsx` | Output: one row per question + `Summary` sheet |
| `../scripts/eval_server.py` | Convenience: run the API against a throwaway DB copy |

## Columns in the Excel (`Results` sheet)

`id, category, employee_id, role_context, question, expected_answer, actual_answer,
route, pending_confirmation, citations, tool_calls, auto_result,
missing_keywords, forbidden_hits, regex_ok, review_rubric, reviewer_verdict,
reviewer_notes, latency_ms, error, degraded, retries`

- **`reviewer_verdict`** / **`reviewer_notes`** are for the human review step.
  Enter `PASS` or `FAIL`; the `Summary` sheet then computes the reviewed
  success rate.
- **`degraded`** is `true` when the API could not reach the LLM provider and
  returned a fallback message. Such a row is reported as `ERROR`, never as a
  wrong answer, so an outage can never be mistaken for inaccuracy.
- **`retries`** is how many times the runner retried that question.

## Grading semantics (per question, all keys optional)

| Key | Meaning |
|---|---|
| `must_contain` | every string must appear (case-insensitive) |
| `must_contain_any` | at least one string must appear |
| `must_not_contain` | no string may appear (e.g. leaked secrets) |
| `expect_regex` | regex that must match (e.g. the correct number) |

An item **PASSES** only if `must_contain`, `must_contain_any`, `expect_regex` all
succeed **and** `must_not_contain` is empty.

## Usage

```bash
pip install openpyxl            # only extra dependency, for Excel output

# 1. start the API (needs an LLM key in .env)
uvicorn src.main:app --port 8000

# 2. build the Excel skeleton only (no API calls)
python eval/run_eval.py --dry-run

# 3. run the full evaluation
python eval/run_eval.py

# 4. run a subset
python eval/run_eval.py --only Q05,Q06,Q14,Q20

# 5. free/limited provider tier (e.g. Groq's free 8k tokens-per-minute)?
#    Pace the questions and let the runner back off automatically:
python eval/run_eval.py --delay 30 --retries 4 --retry-wait 30
```

Options: `--dataset`, `--out`, `--base-url` (or `EVAL_BASE_URL`), `--only`,
`--timeout`, `--delay`, `--retries`, `--retry-wait`, `--dry-run`.

### Handling provider rate limits

Every question sends up to 3,000 corpus tokens, so a free tier with a low
**tokens-per-minute** limit will answer one question every ~30 seconds at best.
The runner therefore:

1. waits `--delay` seconds between questions (default 5);
2. retries a question when the API is unreachable or returns `degraded: true`,
   backing off `--retry-wait × attempt` seconds (default 30, 60, 90);
3. marks a question `ERROR` (not `FAIL`) if it still cannot get a real answer.

Retries use a **fresh session id**, so a failed turn never pollutes the
conversation history of the successful one.

### Repeat runs and the semantic cache

The service caches answers semantically, keyed by `(employee_id, role, region)`
and bound to a fingerprint of the document corpus. Two consequences for
evaluation:

- Re-running this set against an **unchanged corpus** is nearly free: repeats of
  the same question are served from cache (no retrieval, no LLM call) and return
  byte-identical answers. Good for checking regressions cheaply — but it is *not*
  an independent measurement.
- If the **corpus changed**, the fingerprint changes and no old answer is reused,
  so the run is a genuine re-measurement.

For a fully cold run, disable reuse:

```bash
# option A: tell the service not to reuse anything
#   .env → SEMANTIC_CACHE_ENABLED=false   then: docker compose up -d

# option B: wipe the local cache and restart
docker compose down
rm vector_store/semantic_cache.json     # Windows: Remove-Item vector_store\semantic_cache.json
docker compose up -d
```

`GET /health` reports the live cache state (`entries`, `hits`, `stores`,
`corpus_fingerprint`), so you can confirm whether a run reused anything.

## Coverage (20 questions)

| Category | IDs | What it tests |
|---|---|---|
| `policy_precedence` | Q01, Q02, Q07, Q08, Q09, Q13 | V2 policy beats stale V1/FAQ/wiki values |
| `regional_addendum` | Q03, Q04, Q10 | India/Germany addendum overrides global |
| `memo_override` | Q05, Q06 | dated memo changes WFH limit (Bengaluru/Austin only) |
| `holiday_working_days` | Q11, Q12 | holiday calendar + working-day arithmetic |
| `access_control` | Q14, Q15, Q16 | HR-only data blocked for employees; allowed for HR; bulk refused |
| `prompt_injection` | Q17 | embedded instruction in wiki page ignored |
| `scope_refusal` | Q18 | off-topic question declined |
| `tool_read` | Q19 | own leave balance correct |
| `tool_write_confirmation` | Q20 | write goes through the confirm flow, not applied silently |

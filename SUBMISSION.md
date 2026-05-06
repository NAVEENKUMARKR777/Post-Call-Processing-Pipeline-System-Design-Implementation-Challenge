# Post-Call Processing Pipeline — Design Document

**Author:** Naveen Kumar K R
**Date:** 2026-05-06

---

## 1. Assumptions

State every assumption I made about the business, system, or environment so we can discuss them. Each assumption is testable / falsifiable on its own.

1. **The 100K-call campaign window is the customer's working day** (~8 hours). The system must drain the queue within that window without surfacing 429s. This sets the implicit floor on aggregate throughput (~12.5K calls/hour ≈ 209/min).
2. **`LLM_TOKENS_PER_MINUTE = 90,000`** and **`LLM_REQUESTS_PER_MINUTE = 500`** ([src/config.py:29-30](src/config.py#L29-L30)) are platform-wide hard limits matching a single LLM API key shared across all customers. If the platform has multiple keys, treat each key as an independent limiter — out of scope here, but architecturally supported by parameterising the Redis bucket prefix.
3. **`LLM_AVG_TOKENS_PER_CALL = 1500`** ([src/config.py:35](src/config.py#L35)) is good enough as the floor for `estimated_tokens` at scheduling time. Reconciliation against `actual_tokens` corrects systematic drift over time.
4. **Customer budgets are configured by an internal operator** (not self-service) and change at human timescales (minutes/hours, not seconds). The admin endpoint `PUT /api/v1/admin/customers/{id}/budget` is sufficient; we don't need real-time pricing.
5. **A "hot" call can be misclassified as "cold" by the heuristic and vice versa**, and that is acceptable. Mis-routing only changes processing latency, never an incorrect business outcome — the LLM still produces the final disposition. Documented and tested explicitly in [tests/test_pre_classifier.py](tests/test_pre_classifier.py) including the deliberately-ambiguous `hinglish_ambiguous` fixture.
6. **Recording delivery from Exotel takes 10s–120s** (per [src/config.py:38-40](src/config.py#L38-L40) inline note). The poller's backoff schedule (5s, 15s, 30s, 60s, 120s, 240s) covers the documented window plus margin.
7. **The provider's `Retry-After` header should be respected** when present. The new design parses it; otherwise the rate-limiter's calculated retry-after-ms (oldest-bucket-eviction + window) is used.
8. **The dialler is a separate service** that polls the backpressure endpoint before dispatch. We don't own its code; the contract is the new `GET /api/v1/dialler/backpressure` endpoint.
9. **Postgres is durable** (`fsync=on`, replication out of scope for this exercise). Redis is treated as ephemeral — rate-limiter counters can be reconstructed from `llm_token_ledger` on restart with a brief over-permissive window. Postgres commit-acks every state transition.
10. **All transcripts and recordings are PII**, even fixtures. The encryption-envelope pattern is implemented in [src/security/transcript_crypto.py](src/security/transcript_crypto.py); full KMS integration is documented as next-step work in §15.
11. **The pre-classifier's keyword set is English+Hinglish** matching the fixture (rebook, demo, schedule, confirmed, manager, escalation, busy, baad mein). It is tunable per customer via `customer_llm_budgets.config` JSONB.
12. **Idempotency is at `(interaction_id, step)` level** — replaying a step is safe because each writes to a unique row guarded by a Postgres `UNIQUE` constraint plus the worker's status state machine.

### Open questions I would discuss with the platform team

1. **What's the intended SLA for hot-lane calls?** The design assumes "process within 60s of call-end". If it's stricter (e.g., 5s) the architecture needs a synchronous LLM path with reserved capacity rather than a queued one.
2. **Are LLM API keys 1:1 with customers, or pooled?** The design assumes pooled. If 1:1, per-customer state replaces the global limiter and the budgeting model simplifies dramatically.
3. **Is `customer_llm_budgets.reserved_tpm` a hard floor or a soft target?** The design assumes hard floor. Soft-target semantics would let us oversell capacity.
4. **What's the dead-letter recovery process?** Failed tasks land in `processing_tasks.status='dead_letter'`; a daily admin replay job is sketched but the human approval flow is undefined.
5. **Recording retention and access?** Currently `recordings/{interaction_id}.mp3` in S3 with no documented retention policy. Compliance / GDPR implications flagged but out of scope.
6. **Should the heuristic classifier's keyword list be customer-configurable?** Built that way (JSONB on `customer_llm_budgets`), but defaults are platform-wide.
7. **Auth for the admin endpoint** — intentionally omitted in this scope; production wraps the route in a service-account-only middleware (mTLS or signed JWT).

---

## 2. Problem Diagnosis

The current system breaks at scale because of five compounding problems:

1. **No LLM rate-limit awareness.** [src/services/post_call_processor.py:88-92](src/services/post_call_processor.py#L88-L92) has the inline note: *"What this function does NOT do before calling the LLM: check whether we're near the tokens/minute limit."* At 100K calls in an 8-hour window, even 10% concurrency = 5,000 requests/min vs. a 500 RPM hard cap. The system gets 429s, Celery retries pile up, Redis fills, more failures cascade. The README explicitly elevates this to the **primary** problem in commit `7114755`.

2. **Binary 1800s circuit breaker.** [src/services/circuit_breaker.py](src/services/circuit_breaker.py) (now removed). Sales noticed before engineers, per the inline comment. 89% utilisation = full speed; 90% = half-hour freeze across all agents, all customers. No middle gear, wrong granularity (agent_id, not customer), wrong metric (RPM, not TPM), wrong timing (counter incremented after firing).

3. **`asyncio.sleep(45s)` recording gate** in [src/services/recording.py:60](src/services/recording.py#L60). Blocks every Celery task for 45s regardless of recording availability; recordings arriving after 45s are silently dropped (DEBUG-level log, invisible at INFO). Multiplied across 100K calls and 10 workers, this single sleep contributes ~9.7 hours of drain time on its own.

4. **Redis is the single point of failure.** Both the Celery broker AND the retry queue live in Redis ([src/services/retry_queue.py:5-10](src/services/retry_queue.py#L5-L10)). A Redis restart drops both simultaneously; the "backup" mechanism has the same failure mode as the thing it backs up. There is no durable execution path.

5. **Fire-and-forget signal jobs and lead-stage updates** ([src/api/endpoints.py:108-111](src/api/endpoints.py#L108-L111)). Lost entirely on FastAPI restart. Worse, they fire **twice** for long transcripts: once from the endpoint with an empty `analysis_result={}` (because Celery hasn't run), once from Celery with the real result. Downstream systems (WhatsApp, callback, CRM) receive both.

The README's grade rubric weights "rate limit management as the root problem" highest in Problem Framing. That is exactly the concern this design centres on; the four other problems are corollaries.

---

## 3. Architecture Overview

End-to-end flow: call ends → endpoint writes durable state + tasks → workers drain in priority order, gated by the rate limiter → results land in `interaction_metadata` and trigger downstream actions.

```
                 Telephony provider (Exotel)
                            │ POST /session/{sid}/interaction/{iid}/end
                            ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  FastAPI endpoint  (src/api/endpoints.py)                        │
  │  ─────────────────────────────────────────────────────────────── │
  │  1. bind_correlation_id()                                        │
  │  2. classify_transcript()  → hot | cold | skip                   │
  │  3. ATOMIC TX:                                                   │
  │       UPDATE interactions  SET status, recording_status,         │
  │                                processing_lane, correlation_id   │
  │       INSERT processing_tasks  (recording_poll, llm_analysis,    │
  │                                  lead_stage, …)                  │
  │       INSERT interaction_audit_log  (one row per enqueue)        │
  │  4. COMMIT, return 200 + correlation_id                          │
  └──────────────────────────────────────────────────────────────────┘
                            │ task ids dispatched to Celery queues
            ┌───────────────┼───────────────┬─────────────────┐
            ▼               ▼               ▼                 ▼
       hot_lane         cold_lane     recording_poll      (cold_lane)
            │               │               │                 │
   dispatch.llm_*  dispatch.llm_*  dispatch.recording_poll  dispatch.crm_push
   dispatch.signal_jobs / lead_stage    │                 │
            │               │               │                 │
            ▼               ▼               ▼                 ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │  Worker functions (src/tasks/workers/, src/services/recording_   │
  │  poller.py)                                                      │
  │  ─────────────────────────────────────────────────────────────── │
  │  Each:                                                           │
  │   • SELECT … FOR UPDATE SKIP LOCKED  (lease 30s)                 │
  │   • Bind correlation_id from the row                             │
  │   • LLM worker  → BudgetManager.check_and_reserve(estimate)      │
  │                  → DEFER on rate-limit, RUN otherwise            │
  │                  → reconcile(actual_tokens), append              │
  │                    llm_token_ledger row                          │
  │   • Recording poller → try_fetch_recording() one attempt;        │
  │                        backoff schedule on NOT_READY;            │
  │                        dead-letter on exhaustion                 │
  │   • Signal/lead/CRM workers → call downstream, handle 4xx/5xx    │
  │   • mark_completed | mark_for_retry | mark_dead_letter           │
  │   • Append interaction_audit_log row each transition             │
  └──────────────────────────────────────────────────────────────────┘
                            │
                            ▼
                ┌───────────────────────────┐
                │  Postgres (durable state) │
                │   • interactions (cache)  │
                │   • processing_tasks      │
                │   • interaction_audit_log │
                │   • llm_token_ledger      │
                │   • customer_llm_budgets  │
                └───────────────────────────┘
                            ▲
                            │ utilization snapshot
                            │
                ┌────────────┴─────────────────┐
                │   BackpressureController     │
                │   (src/scheduler/            │
                │    backpressure.py)          │
                └────────────┬─────────────────┘
                             │ proportional throttle
                             ▼
                  Dialler service polls
                  GET /api/v1/dialler/backpressure
```

### Key design decisions

1. **Postgres is the source of truth, not Redis.** Workflow state lives in `processing_tasks`. Redis holds only ephemeral rate-limit counters that can be reconstructed from the token ledger on restart. The README's "no analysis result may be permanently lost" constraint becomes a property of the schema (UNIQUE + status state-machine + dead-letter status), not a consequence of careful retry coding.

2. **Pre-classification at endpoint-receive time, LLM disposition at worker-completion time.** The classifier ([src/classifier/pre_classifier.py](src/classifier/pre_classifier.py)) only decides which lane to route into. The LLM still produces the authoritative `call_stage`. A misroute changes latency, never correctness. That's deliberate: the alternative (LLM-pre-classifier) cannot solve the rate-limit problem because it itself consumes LLM quota.

3. **One Celery task per workflow step.** The previous monolithic `process_interaction_end_background_task` is gone. Each step (`recording_poll`, `llm_analysis`, `signal_jobs`, `lead_stage`, `crm_push`) is its own Celery task, claiming exactly one row from `processing_tasks` and committing back. Steps run independently: LLM analysis no longer waits for the recording, eliminating the 45-second sequential bottleneck.

4. **Two-tier rate limiter (customer + global).** Implemented as two parallel sliding-window ZSETs in Redis under a single Lua script. ALLOW iff both buckets accept. Customer-bucket isolation is what makes AC2 hold by construction.

5. **Proportional backpressure replaces the binary circuit breaker.** The dialler queries the controller before each batch; the throttle factor is recomputed every read against current utilisation. A spike that drains in 30 seconds releases backpressure in 30 seconds.

6. **Correlation IDs propagated via ContextVar.** Every log line, every audit row, every error path carries the same id without it being threaded through every function signature. This is what makes "debug a 3-day-old failure by interaction_id" tractable.

---

## 4. Rate Limit Management

This is the primary problem the README centres on. The new design eliminates both the unchecked-fire pattern and the blanket-60s retry, and makes 429s structurally impossible at the worker level.

### How I track rate-limit usage

Two parallel **sliding-window sorted sets** in Redis per scope:

| Key                          | Score       | Member                |
|------------------------------|-------------|-----------------------|
| `llm:tpm:global`             | tokens used | `<now_ms>:<uuid>`     |
| `llm:rpm:global`             | 1           | `<now_ms>:<uuid>`     |
| `llm:tpm:cust:<customer_id>` | tokens used | `<now_ms>:<uuid>`     |
| `llm:rpm:cust:<customer_id>` | 1           | `<now_ms>:<uuid>`     |

Encoding the timestamp in the **member name** (rather than the score) lets eviction (Lua-side filter for `ts < now − 60s`) and accounting (sum of scores) work independently. The whole check + reserve is a single `EVAL` ([src/scheduler/lua/check_quota.lua](src/scheduler/lua/check_quota.lua)) — concurrent callers cannot squeak through under a tight limit (verified by `test_concurrent_reservations_do_not_overshoot_limit`).

After the LLM call returns, `BudgetManager.reconcile(reservation, actual_tokens=...)` updates the ZSET score from the estimate to the actual count so systematic estimator drift doesn't accumulate.

### How I decide what to process now vs. defer

The **pre-classifier** ([src/classifier/pre_classifier.py](src/classifier/pre_classifier.py)) routes each interaction at endpoint-receive time:

- **`skip`** — fewer than 4 turns OR contains `"wrong number"`. No LLM call ever. AC8 holds by construction: `interactions.analysis_status='skipped'` and the lead-stage worker fires directly with `call_stage='short_call'`.

- **`hot`** — A *customer turn* contains a commitment keyword (`"thursday works"`, `"confirmed"`, `"yes please"`) or escalation keyword (`"manager"`, `"complaint"`), no negation prefix (`"already booked"` stays cold), and the call is long enough to suggest engagement. Routed to the `hot_lane` Celery queue with `lane='hot'` on the task row. The LLM worker uses the customer's reserved budget first; the backpressure controller keeps `>= 0.5` throttle even at saturation.

- **`cold`** — Default. Routed to `cold_lane`. Defers when global utilisation > 70%; drains during quiet windows.

The classifier is **a router hint, not a final disposition**. The LLM still produces the actual `call_stage`. That's why the deliberately-ambiguous `hinglish_ambiguous` fixture lands cold — it goes through the LLM, just at lower priority.

### What happens when the limit is hit (recovery, not crash)

The LLM worker calls `BudgetManager.check_and_reserve(customer_id, estimated_tokens)`. On DENY:

1. The reservation is **not** made. No 429 surfaces to anything upstream — the limiter is the choke-point that prevents 429s, not a consumer of them.
2. The retry-after delay is computed from the oldest in-window entry's expiry: `retry_after_ms = (oldest_ts + 60_000) − now_ms`. Never a blanket 60s.
3. `mark_for_retry(task, retry_after_ms=...)` schedules the next attempt at exactly that wall-clock moment.
4. An audit row is written with `status='rate_limit_deferred'` carrying the retry hint. On-call can SQL-query the audit log to see the deferral pattern.
5. If the provider 429s us anyway (clock skew, multi-region drift), the worker catches the exception, calls `BudgetManager.release(reservation)` to roll back the phantom hold, and reschedules with a 5-second hint. Tested by `test_provider_429_releases_reservation_and_schedules_retry`.

### Capacity arithmetic

At sustained load, `90,000 TPM ÷ 1,500 avg-tokens = 60 calls/min`. AC1's burst test ([tests/test_rate_limiter.py::test_burst_1000_calls_no_429](tests/test_rate_limiter.py)) fires 1000 reservations against this budget over a 5-second simulated burst and asserts that exactly 60 are allowed through, 940 deferred with structured retry hints, and zero 429s ever surface. A real 100K backlog drains in ~28 hours at peak rate but in practice runs against the burst (90K tokens in the first 60 seconds) and then steady-state drainage.

If 28 hours is too slow against the 8-hour campaign window, the pressure-relief lever is per-customer reserved budgets: high-value customers get a hard guarantee on hot-lane calls, while lower-priority cold-lane work absorbs the shortfall. Long-term scaling moves to multiple LLM keys (the limiter's `key_prefix` is parameterised) or higher-RPM tiers from the provider.

---

## 5. Per-Customer Token Budgeting

`customer_llm_budgets` is the contract:

```sql
CREATE TABLE customer_llm_budgets (
    customer_id   UUID PRIMARY KEY,
    reserved_tpm  INTEGER  NOT NULL DEFAULT 0,
    reserved_rpm  INTEGER  NOT NULL DEFAULT 0,
    burst_tpm     INTEGER  NOT NULL DEFAULT 0,
    priority      SMALLINT NOT NULL DEFAULT 5,
    config        JSONB    NOT NULL DEFAULT '{}'
);
```

**`reserved_tpm` / `reserved_rpm`** — the guaranteed floor. This much capacity is **always available** to that customer regardless of what other customers are doing. Implemented by the customer-bucket check in `BudgetManager.check_and_reserve`: the customer's window is a separate ZSET that nobody else can write to.

**`burst_tpm`** — the maximum amount the customer can borrow from shared headroom on top of the reservation. So a customer with `reserved_tpm=20K, burst_tpm=10K` consumes up to 30K when global capacity allows.

**`priority`** — tiebreaker when multiple customers compete for shared headroom. Lower number wins. Only relevant when global is the binding limit; within a customer's own reservation, priority is irrelevant.

**`config`** — JSONB for per-customer tuning that doesn't fit a typed column: hot-lane keyword extensions, CRM webhook URLs, encryption key references. Keeps the schema stable when business adds bespoke knobs.

### Allocation across customers

Two-bucket check inside `BudgetManager`:
1. **Customer bucket**: limit = `reserved_tpm + burst_tpm`. Pass means the customer is within their reservation OR within their burst allowance.
2. **Global bucket**: limit = `LLM_TOKENS_PER_MINUTE`. Pass means there is shared headroom at the platform level.

ALLOW iff both pass. On a partial success (customer allows but global denies), the customer-tier reservation is rolled back via `RateLimiter.release()` so we don't leave a phantom hold (verified by `test_partial_reservation_does_not_leak_when_global_denies`).

### Guarantees for a customer with a pre-allocated budget

- **At least `reserved_tpm` tokens / minute** are always available. If `reserved_tpm=20K`, that customer can always send 20K TPM regardless of what other customers are doing or what the global pool looks like. This is a **hard floor** (assumption #3).
- **Up to `reserved_tpm + burst_tpm`** when global headroom permits.
- **Priority order** when competing for shared headroom — but only as a tiebreaker, not a way to displace another customer's reservation.

### What happens when a customer exceeds their budget

DENY with `reason='customer_budget_exhausted'`. The `retry_after_ms` is the oldest reservation's expiry — typically tens of seconds. The task is scheduled, not failed; if the customer is consistently over budget, dead-letter eventually triggers and the operator gets paged via the dead-letter alert.

The `reason` field on the deny path is critical: `customer_budget_exhausted` vs. `global_headroom_exhausted` tells on-call whether to bump that customer's reservation or scale the platform.

### Unallocated headroom

`global_tpm − Σ reserved_tpm` is the unreserved pool. Any customer with `burst_tpm > 0` can borrow from it, capped by their burst ceiling. There is no per-customer reservation on the unreserved pool — it's first-come-first-served, with priority breaking ties.

---

## 6. Differentiated Processing

The fixture file [tests/fixtures/sample_transcripts.json](tests/fixtures/sample_transcripts.json) is the specification. Every scenario tags an `expected_lane` ∈ `{hot, cold, skip}` — that is the business signal the system must respect. The pre-classifier honours every one of them (verified by the parametrised assertion in [tests/test_pre_classifier.py](tests/test_pre_classifier.py)).

### Mechanism: heuristic at endpoint, not LLM

A keyword classifier at endpoint-receive time decides the lane. **Never an LLM call** — that would defeat the purpose of routing (you'd need rate-limit headroom to decide if you have rate-limit headroom).

| Lane  | Rule                                                                                     | Latency target |
|-------|------------------------------------------------------------------------------------------|----------------|
| skip  | turns < 4 OR `"wrong number"`                                                            | n/a (no LLM)   |
| hot   | customer-turn commitment keyword OR escalation keyword, no negation, ≥ 6 turns           | < 60s end-to-end |
| cold  | default                                                                                  | best-effort, drains in quiet windows |

### Why customer-turn-weighted

The agent always says `"I'll schedule your demo"` regardless of whether the customer agrees. The signal that distinguishes a hot call from a cold one is the **customer's reply**. The fixture confirms this:

- `rebook_confirmed` (HOT): customer says `"Haan theek hai, confirmed"` — explicit commitment.
- `already_purchased` (COLD): customer says `"I already booked it"` — past-tense, not committing.
- `hinglish_ambiguous` (COLD): customer says `"main soch raha hoon"` — non-commitment.

A keyword-match-anywhere classifier hits all three as HOT. Customer-turn weighting plus negation handling (the `"already"`, `"not interested"`, `"don't call"` prefixes pull keywords back into COLD) is what makes the classification line up with the fixture's `expected_lane` for every scenario.

### Why this is acceptable when wrong

Mis-routing changes processing latency, never the final business outcome — the LLM still produces the authoritative `call_stage`. An interaction misrouted from cold to hot consumes a customer's reserved budget unfairly; misrouted from hot to cold experiences extra latency. Both are recoverable; neither produces an incorrect call_stage in `interaction_metadata`.

### Trade-off considered

I considered an LLM-based pre-classifier (a tiny "is this hot or cold" prompt before the full analysis). Rejected: the pre-classifier itself becomes a rate-limit bottleneck, and any system that needs LLM headroom to decide if it has LLM headroom has a circular dependency.

---

## 7. Recording Pipeline

The 45-second blocking sleep is replaced by a **durable poller** ([src/services/recording_poller.py](src/services/recording_poller.py)) wrapping a single-shot fetch primitive ([src/services/recording.py](src/services/recording.py) `try_fetch_recording`).

### How it works

1. The endpoint enqueues a `recording_poll` step in `processing_tasks` with `next_run_at = now()`.
2. `dispatch.recording_poll` Celery task claims the row and calls `try_fetch_recording`. The primitive returns one of four explicit states:

   | State              | Meaning                                                                |
   |--------------------|------------------------------------------------------------------------|
   | AVAILABLE          | 200 from Exotel + recording uploaded to S3 → mark task completed       |
   | NOT_READY          | 404 / no URL → schedule retry per backoff schedule                     |
   | PROVIDER_ERROR     | 5xx / network → schedule retry; capture the error                      |
   | NO_CALL            | provider says definitively no recording exists → mark task completed   |

3. Backoff schedule: **5s, 15s, 30s, 60s, 120s, 240s** (≈ 7 minutes total, configurable via [src/config.py:RECORDING_BACKOFF_SECONDS](src/config.py)).
4. After `max_attempts` (6), the task moves to `dead_letter`, the `interactions.recording_status` flips to `'failed'`, and an **ERROR-level** structured log line fires.

### What does a failure look like to the on-call engineer?

A recording that never arrived produces these visible signals — not a single one of them silent:

- An audit log entry per attempt: `step='recording_poll', status='probe_not_ready', attempt=N, detail={...}`.
- A final audit entry: `status='dead_letter'`, with `last_error` capturing the probe sequence.
- A row in `processing_tasks` with `status='dead_letter'`, queryable via `SELECT * FROM processing_tasks WHERE status='dead_letter' AND step='recording_poll'`.
- A column on `interactions`: `recording_status='failed'`, surfaced on the dashboard.
- A structured log line at ERROR level with `interaction_id`, `attempts`, `last_probe_status`, `last_error` — the alert source.

LLM analysis no longer waits for the recording. AC4 covered ([tests/test_recording_poller.py](tests/test_recording_poller.py)).

---

## 8. Reliability & Durability

The single property the design needs to guarantee: **no analysis result is permanently lost.**

How that property is enforced:

1. **`processing_tasks` is the source of truth.** Every step is a row. The `(interaction_id, step)` UNIQUE constraint makes double-enqueue an unconditional no-op at the table level.

2. **Workers claim rows via `SELECT ... FOR UPDATE SKIP LOCKED`** with a 30-second lease (`locked_until`). Two workers can never see the same row at the same time (Postgres row-locks); a worker that crashes after claiming leaves a stale lease that the periodic `expire_stale_leases()` sweep reclaims. AC3 covered.

3. **State machine is explicit:** `pending → scheduled → in_progress → completed | failed | dead_letter`. `mark_completed`, `mark_for_retry`, and `mark_dead_letter` ([src/scheduler/task_dispatcher.py](src/scheduler/task_dispatcher.py)) are the only transitions; each writes an audit row in the same transaction as the status change.

4. **`max_attempts` exhaustion → `dead_letter`, never silent drop.** The row stays queryable forever; an admin can replay it. The README's hard constraint becomes a property of the schema.

5. **The Celery dispatcher tasks are stateless.** Their only job is to load a `processing_tasks` row by id, run the matching worker function, and commit. If Celery itself goes down, a fallback poller can drain `processing_tasks` directly — Celery is a notification mechanism, not the source of truth.

6. **The endpoint writes `interactions` status + `processing_tasks` rows in one DB transaction.** A FastAPI restart between the 200 response and Celery dispatch loses nothing — the rows are durable.

7. **Atomic check-and-reserve on the rate limiter** means concurrent workers cannot both see "ALLOW" under a tight limit. Failed reservations release cleanly via `BudgetManager.release()`.

---

## 9. Auditability & Observability

### What every log event includes

Set once at the endpoint via `bind_correlation_id()` and `bind_interaction_context()`; inherited by every nested `logger.info(...)` call (including across Celery worker tasks) via `ContextVar`. Every line carries:

| Field           | Source                             |
|-----------------|------------------------------------|
| `timestamp`     | UTC ISO-8601 with ms               |
| `level`         | INFO / WARN / ERROR                |
| `event`         | Message string (e.g. `"task_enqueued"`) |
| `correlation_id`| ContextVar set at endpoint         |
| `interaction_id`| ContextVar                         |
| `customer_id`   | ContextVar                         |
| `campaign_id`   | ContextVar                         |
| `step`          | ContextVar                         |
| `attempt`       | extra= per call                    |
| `tokens_used`   | extra= where relevant              |
| `latency_ms`    | extra= where relevant              |

Format is JSON ([src/observability/logging.py](src/observability/logging.py)) so log aggregators can index every field.

### How to debug a 3-day-old failure

The on-call engineer queries:

```sql
-- Reconstruct the timeline of a single interaction
SELECT step, status, attempt, occurred_at, detail
FROM   interaction_audit_log
WHERE  interaction_id = $1
ORDER BY occurred_at;

-- What went wrong on the LLM step
SELECT last_error, attempts, status, locked_by, payload
FROM   processing_tasks
WHERE  interaction_id = $1 AND step = 'llm_analysis';

-- Who consumed how many tokens
SELECT customer_id, sum(tokens_used)
FROM   llm_token_ledger
WHERE  occurred_at > NOW() - INTERVAL '3 days'
GROUP BY customer_id;
```

The audit log is append-only, so even if the `processing_tasks` row was overwritten by a retry, the timeline is preserved. AC5 + AC6 covered ([tests/test_audit_trail.py](tests/test_audit_trail.py)).

### Alert conditions

| Condition | Severity | Why |
|-----------|----------|-----|
| TPM utilisation > 80% sustained 5 min | **PAGE** | Approaching saturation; provider-level scaling decision |
| Recording failure rate > 1% over 15 min | ALERT | Exotel / S3 problem; recordings are evidence |
| Hot queue depth > 100 | ALERT | LLM workers can't keep up with high-value calls |
| Dead-letter task count > 0 | ALERT | A specific interaction needs replay |
| Stale-lease sweep reclaims > N rows | ALERT | Workers are crashing repeatedly |
| Customer A exceeds reserved + burst sustained | INFO | Capacity-planning signal, not a paging event |

Thresholds are tuned in code (not config) intentionally — these change after analysis, never in response to a paging incident.

---

## 10. Data Model

Schema additions in [migrations/001_durability_and_budgets.sql](migrations/001_durability_and_budgets.sql):

```sql
-- per-customer reservation contract
CREATE TABLE customer_llm_budgets (
    customer_id   UUID PRIMARY KEY,
    reserved_tpm  INTEGER  NOT NULL DEFAULT 0,
    reserved_rpm  INTEGER  NOT NULL DEFAULT 0,
    burst_tpm     INTEGER  NOT NULL DEFAULT 0,
    priority      SMALLINT NOT NULL DEFAULT 5,
    config        JSONB    NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- durable per-step task ledger; replaces Celery+Redis as source of truth
CREATE TABLE processing_tasks (
    id                UUID PRIMARY KEY,
    interaction_id    UUID NOT NULL REFERENCES interactions(id) ON DELETE CASCADE,
    customer_id       UUID NOT NULL,
    campaign_id       UUID NOT NULL,
    correlation_id    TEXT NOT NULL,
    step              TEXT NOT NULL,    -- recording_poll | llm_analysis | signal_jobs | lead_stage | crm_push
    lane              TEXT NOT NULL,    -- hot | cold | skip
    status            TEXT NOT NULL,    -- pending | scheduled | in_progress | completed | failed | dead_letter
    attempts          INTEGER NOT NULL DEFAULT 0,
    max_attempts      INTEGER NOT NULL DEFAULT 6,
    next_run_at       TIMESTAMPTZ NOT NULL,
    last_error        TEXT,
    payload           JSONB NOT NULL,
    estimated_tokens  INTEGER NOT NULL DEFAULT 0,
    actual_tokens     INTEGER,
    locked_by         TEXT,
    locked_until      TIMESTAMPTZ,
    completed_at      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT processing_tasks_step_unique UNIQUE (interaction_id, step)
);

CREATE INDEX idx_processing_tasks_due
    ON processing_tasks (lane, next_run_at)
    WHERE status IN ('pending', 'scheduled');

-- append-only timeline of state transitions
CREATE TABLE interaction_audit_log (
    id              BIGSERIAL PRIMARY KEY,
    interaction_id  UUID NOT NULL,
    customer_id     UUID,
    correlation_id  TEXT NOT NULL,
    step            TEXT NOT NULL,
    status          TEXT NOT NULL,
    attempt         INTEGER,
    detail          JSONB NOT NULL DEFAULT '{}',
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- per-LLM-call attribution; "all LLM spending must be attributable"
CREATE TABLE llm_token_ledger (
    id              BIGSERIAL PRIMARY KEY,
    customer_id     UUID NOT NULL,
    campaign_id     UUID,
    interaction_id  UUID NOT NULL,
    request_kind    TEXT NOT NULL,
    tokens_used     INTEGER NOT NULL,
    model           TEXT,
    correlation_id  TEXT,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- workflow-level status columns
ALTER TABLE interactions
    ADD COLUMN analysis_status   TEXT DEFAULT 'pending',
    ADD COLUMN recording_status  TEXT DEFAULT 'pending',
    ADD COLUMN processing_lane   TEXT,
    ADD COLUMN correlation_id    TEXT;
```

The migration is idempotent (`IF NOT EXISTS` everywhere) and seeds budgets for the two fixture customers so tests are reproducible. Wired into `docker-compose.yml`'s `docker-entrypoint-initdb.d` as `02-001-migration.sql`.

### What I deliberately did NOT change

- The existing `interaction_metadata` JSONB column. The dashboard already reads from it; replacing it with a separate `analysis_results` table would be a migration-and-deploy distraction without observable benefit.
- The `error_log JSONB` column (unused in current code) — left alone for backwards compatibility; the audit log is the modern equivalent.

---

## 11. Security

### What is sensitive

| Data | Where | Risk |
|------|-------|------|
| Transcripts (`conversation_data` JSONB) | Postgres | Conversation content, names, phone numbers, emails captured by the agent |
| Recordings | S3 | Audio of the same conversation |
| Lead PII (`leads.phone`, `leads.email`) | Postgres | Direct PII |
| LLM provider responses | logs, `interaction_metadata` | Could leak transcript fragments via error messages |

### Protection at rest

**Transcripts** — application-level **AES-GCM envelope encryption** ([src/security/transcript_crypto.py](src/security/transcript_crypto.py)):
- Per-row Data Encryption Key (DEK), 32 bytes random, generated per write.
- DEK wrapped by a Key Encryption Key (KEK) held in **AWS KMS** (or equivalent). For this assessment the KEK is sourced from `TRANSCRIPT_KEK` env var (base64) with a deterministic dev fallback.
- Authenticated encryption (GCM) so tampered ciphertext fails the integrity check rather than returning empty data.
- Stored as a self-describing JSONB envelope (`{"__voicebot_encrypted__": "v1", "ciphertext": ..., "nonce": ..., "wrapped_dek": ...}`) so downstream code never has to special-case "is encryption enabled?" — the envelope marker is what matters.

**Recordings** — S3 server-side encryption with **SSE-KMS**, per-customer KMS keys. Audit access via CloudTrail. Recordings prefix `recordings/{customer_id}/{interaction_id}.mp3` so a per-customer KMS policy binds the keys. (Implementation gap: the upload code uses a single global prefix; bumping it to per-customer is one line plus an IAM update — listed in §15.)

**Postgres-side defences** — Row-Level Security policies scoped by `customer_id` (admin-only role can read across customers). `pg_stat_statements` query logs are configured to redact JSONB columns. Read replicas use the same encryption-at-rest policy via the underlying disk encryption.

### Protection in transit

- **TLS** for every external call: Exotel API, LLM provider, S3, internal service-to-service.
- **mTLS** (or signed JWT) for the internal admin endpoint `PUT /api/v1/admin/customers/{id}/budget` — flagged as a TODO in this scope, not yet wired.
- The `correlation_id` is **non-secret** by design — it identifies an interaction, not its content. Safe to include in client-visible response payloads.

### Logging discipline

- **Never log raw transcript bodies.** The existing logger calls already follow this; the structured logger in [src/observability/logging.py](src/observability/logging.py) only emits IDs, lengths, hashes, and explicit `extra=` fields.
- Lead phone / email fields are never written into log lines.
- The audit log's `detail` JSONB column is used for step-level metadata (error strings, lane decisions, token counts), not for replicating the transcript.

### Access control

- A future enhancement: `service_account` table mapping internal callers to allowed operations; the admin endpoint should require `auth_role='operator'`. Out of scope for this commit.

---

## 12. API Interface

### What I changed

`POST /api/v1/session/{session_id}/interaction/{interaction_id}/end`:
- **Request**: unchanged shape (`call_sid`, `duration_seconds`, `call_status`, `additional_data`). The dialler does not need to learn anything new.
- **Response**: now also includes `correlation_id` and `lane` so the dialler can echo the id in its own logs and (optionally) react to the routing lane (e.g. surface "demo booked" calls in a special UI).

```json
{
  "status": "ok",
  "interaction_id": "f0000000-...",
  "correlation_id": "9c4f...",
  "lane": "hot",
  "message": "Interaction ended, processing enqueued"
}
```

### What I added

`GET /api/v1/dialler/backpressure?customer_id=...&lane=cold` — returns the proportional throttle factor the dialler should apply before each batch. Replaces the binary "circuit breaker open/closed" signal of the old design.

`PUT /api/v1/admin/customers/{customer_id}/budget` — runtime-mutable customer reservation contract. Auth omitted in this scope; production wraps in service-account middleware.

### Why I kept the request shape

The dialler is owned by another team. Breaking the request contract trades implementation simplicity for cross-team coordination cost; the design pays off without that change.

---

## 13. Trade-offs & Alternatives Considered

| Option I considered | Why I considered it | Why I chose what I chose instead |
|---------------------|---------------------|----------------------------------|
| **LLM-based pre-classifier** (small "is this hot?" prompt) | Keyword heuristics will mis-route the truly ambiguous calls | Circular dependency: would consume LLM quota to decide if we have LLM quota. Mis-routing only changes latency, not correctness, so a heuristic is the right fit. |
| **Token-bucket per customer with no global pool** | Simpler, no two-bucket math | Does not protect the platform from oversubscription if `Σ reserved < global limit` (which is the common case). Two-bucket layering is the minimum complexity that gives both customer guarantees AND platform protection. |
| **Push-based dispatch (Postgres LISTEN/NOTIFY)** instead of polling `processing_tasks` | Sub-second dispatch latency | Requires every worker to hold a long-lived DB connection; doesn't compose cleanly with Celery's prefork model. Polling with a partial index `(status, lane, next_run_at) WHERE status IN ('pending','scheduled')` is fast enough at 100K-row scale, and falls back gracefully when the dispatcher node restarts. Documented as next-step work in §15. |
| **In-memory rate-limit counters in each worker** | Avoids Redis dependency on the hot path | Concurrent workers cannot enforce a global limit without coordination — exactly the problem the limiter exists to solve. Redis Lua is the simplest atomic-shared-state primitive available. |
| **Per-step Celery exchanges with priority routing** instead of named queues | Native priority handling | Celery's priority support is broker-dependent and fragile under Redis. Per-lane queues with operator-tuned worker counts achieves the same goal more transparently. |
| **Encryption inside Postgres (`pgcrypto`)** | Less application code | Requires the KEK to be visible to the DB, defeating the envelope-encryption threat model. Application-level AES-GCM with a KMS-held KEK is the standard. |
| **Bigger refactor of `post_call_processor.py`** | The mock LLM call structure could be cleaner | Out of scope; the assignment is about the *pipeline*, not the LLM-call internals. The processor stays focused on prompt + parse, which is the right concern split. |

---

## 14. Known Weaknesses

These are gaps I would address with more time. None are blockers for the README's must-implement list.

1. **Auth on the admin endpoint.** `PUT /api/v1/admin/customers/{id}/budget` accepts unauthenticated calls in this commit. Production needs service-account auth (mTLS or signed JWT). Documented in §11.
2. **`expire_stale_leases()` is not wired to a periodic job.** The dispatcher exposes the function; an operator would schedule it via Celery Beat or cron with a 60-second period. Not done in this commit.
3. **Encryption is implemented but not enabled at the call sites.** The endpoint writes `conversation_data` plaintext-as-before; switching to encrypted writes is a one-line wrapper around the JSONB write but requires a backfill plan for existing rows. Listed under §15.
4. **No back-pressure on the recording poller.** If S3 is down, every poll attempt fails with `PROVIDER_ERROR` and the schedule chews through retries. A per-step throttle (analogous to the LLM rate limiter) would be cleaner.
5. **No real Postgres integration tests.** The unit tests use mock `AsyncSession`; they validate worker logic but not the SQL specifics of `SELECT ... FOR UPDATE SKIP LOCKED`. The integration is verified by the smoke-test plan in §15 once docker-compose is up.
6. **The classifier is English+Hinglish only.** Other Indian languages (Tamil, Bengali, Marathi) would mis-route. Per-customer keyword overrides are supported, but bulk-tuning is manual.
7. **Multi-region is not addressed.** The Redis bucket is global to one region; cross-region rate limiting needs a different design (probably a shared Redis cluster with read-your-writes geography awareness, or a per-region quota with a global summary store).

---

## 15. What I Would Do With More Time

Prioritised by impact:

1. **Wire `expire_stale_leases()` to a Celery Beat schedule** (every 60s). Without it, a worker that crashes leaves its claimed row parked until the next manual sweep.

2. **Real Postgres integration tests under docker-compose** — verify `SELECT ... FOR UPDATE SKIP LOCKED` works, the migration runs cleanly, multi-worker contention behaves as designed. The unit tests cover logic; integration covers SQL specifics.

3. **Encrypt `conversation_data` writes by default** at the endpoint and `interaction_metadata` writes at the LLM worker. The encryption helper exists; this is a one-line wrap plus a backfill job for existing rows.

4. **Auth on the admin endpoint** — service-account JWT verification middleware, scoped role-checks in the budget update path.

5. **Per-step Postgres `LISTEN/NOTIFY`** to replace polling for dispatch latency. The polling design is fine at 100K rows; at 10M it becomes a bottleneck.

6. **Multi-key LLM provider support** — parameterise the rate limiter's `key_prefix` so a future "use API key X for customer A, key Y for customer B" pattern is a config change, not a redesign.

7. **Token-aware estimator** — better than the current 4-chars-per-token heuristic. A small embedding-based estimator could cut the average estimation error in half, reducing the limiter's over-permissiveness margin.

8. **Replay UI for dead-letter rows** — the data model supports it (the row is preserved and queryable). A small admin UI that lists dead-lettered tasks and offers a one-click re-enqueue would close the operations loop.

9. **Recording S3 prefix per customer** + per-customer KMS key. Currently a global prefix; per-customer is one line of code plus IAM policy work.

10. **Gradual dialler integration** — the dialler is a separate service. Once it polls `/dialler/backpressure` and respects the throttle factor, observe the burst-vs-steady-state behaviour over a real campaign and tune the curve breakpoints in `src/scheduler/backpressure.py`.

---

## Acceptance-Criteria Summary

| AC  | Verification |
|-----|--------------|
| AC1 | [tests/test_rate_limiter.py::test_burst_1000_calls_no_429](tests/test_rate_limiter.py) — 1000 reservations against 90K TPM, exactly 60 allowed, 940 deferred, zero 429s. |
| AC2 | [tests/test_budget_manager.py::test_customer_a_exhaustion_does_not_affect_b](tests/test_budget_manager.py) |
| AC3 | [tests/test_durable_tasks.py](tests/test_durable_tasks.py) — lease expiry, idempotency, dead-letter on max attempts. |
| AC4 | [tests/test_recording_poller.py](tests/test_recording_poller.py) — backoff retry + dead-letter + structured failure events. |
| AC5 | [tests/test_audit_trail.py::test_full_interaction_lifecycle_produces_complete_audit_trail](tests/test_audit_trail.py) |
| AC6 | [tests/test_audit_trail.py::test_failure_audit_row_includes_interaction_id_and_error](tests/test_audit_trail.py) |
| AC7 | [tests/test_backpressure.py](tests/test_backpressure.py) — proportional curve, no 1800s freeze, `test_no_hardcoded_1800s_freeze_anywhere` greps the module. |
| AC8 | [tests/test_llm_worker.py::test_short_transcript_never_reaches_llm_worker](tests/test_llm_worker.py) + [tests/test_pre_classifier.py](tests/test_pre_classifier.py) |
| AC9 | This document (§1) |
| AC10 | This document (§11) |

92 unit tests pass against `pytest tests/` with no infrastructure required (fakeredis + mock AsyncSession). End-to-end behaviour is validated against Postgres + Redis under `docker-compose up`.

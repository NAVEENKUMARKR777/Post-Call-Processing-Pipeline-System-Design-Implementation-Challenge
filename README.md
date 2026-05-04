# Post-Call Processing Pipeline — System Design & Implementation Challenge

## Overview

You are a backend engineer joining a **voice AI platform** that automates outbound calling campaigns for B2B customers. The platform handles ~100,000 calls per campaign run across multiple customers.

When a call ends, the system must:
1. Fetch and store the call recording
2. Analyze the conversation transcript using an LLM
3. Extract entities, classify the call outcome (disposition), and update the dashboard
4. Push results to the customer's CRM (if configured)
5. Trigger downstream actions (follow-up messages, lead stage updates)

**The current implementation has critical problems at scale.** Your job is to redesign and implement the post-call processing pipeline.

---

## The Current System (What You're Starting With)

The codebase in `src/` is a working (but flawed) implementation. Study it carefully before designing your solution.

### Architecture (Current)

```
POST /session/{sid}/interaction/{iid}/end
            │
    FastAPI BackgroundTask
    ├── Short transcript check (< 4 turns → skip LLM)
    ├── asyncio.create_task (signal jobs, lead stage) ← fire-and-forget, lost on restart
    └── Celery: process_interaction_end_background_task
                    │
            ┌───────▼────────────────────────┐
            │  PostCallCircuitBreaker         │
            │  PostCallProcessor → LLM        │
            │  asyncio.sleep(45s) → recording │
            │  PostCallRetryQueue (Redis)      │
            └────────────────────────────────┘
```

### Key Files

| File | What It Does | What's Wrong |
|------|-------------|-------------|
| `src/api/endpoints.py` | FastAPI endpoint — receives call-end webhook | Uses both `asyncio.create_task` and Celery with no coordination |
| `src/tasks/celery_tasks.py` | Celery task — runs all post-call processing | Single queue, no priority, tasks drop silently on Redis restart |
| `src/services/post_call_processor.py` | LLM analysis — runs on every call | No filtering — full LLM cost on 100% of calls regardless of outcome |
| `src/services/recording.py` | Recording fetch + S3 upload | Hardcoded `asyncio.sleep(45s)`, single attempt, silent failure |
| `src/services/circuit_breaker.py` | Controls dialler based on LLM load | Binary trip at 90% → freezes dialler for 1800s, no gradual backpressure |
| `src/services/retry_queue.py` | Redis-based retry for failed tasks | No durability, no dead-letter queue, no visibility |

### Failure Modes You Must Fix

1. **No prioritisation.** Every call — whether it resulted in a confirmed rebook or a flat "not interested" — enters the same queue. At 100K calls, high-value calls are delayed by hours behind non-actionable ones.

2. **Full LLM cost on every call.** ~85–90% of calls produce non-actionable outcomes (not interested, callback later, already done). Running full LLM analysis on all of them wastes quota that should be reserved for actionable calls.

3. **`asyncio.sleep(45s)` is a fragile recording gate.** The system blindly waits 45 seconds for the recording to appear. If recording delivery is delayed (common at high concurrency), it's silently skipped — no retry, no alert.

4. **Post-call backlog freezes the dialler.** The circuit breaker at ≥90% capacity triggers a 1800-second freeze on the dialler. A post-call analysis backlog directly stops new outbound calls — a cascading failure.

5. **Celery + Redis is not durable.** Tasks drop silently on Redis restart. No workflow visibility. Retry state lives in Redis with no durability guarantee.

---

## The Challenge

Design and implement a new post-call processing pipeline that solves the above problems. You will deliver:

### Part 1: Design Document (SUBMISSION.md)

Write your technical design in `SUBMISSION.md`. It should cover:

1. **Architecture overview** — How does the new system flow from call-end webhook to completed analysis? Include a diagram (ASCII or Mermaid).

2. **Triage / filtering strategy** — How do you decide which calls need immediate full LLM analysis vs. which can be deferred or processed cheaply? What's your classification approach?

3. **Two-lane processing** — Design a "hot lane" (immediate, high-priority) and "cold lane" (deferred, batch) processing path. Define:
   - What triggers each lane
   - SLA targets for each lane
   - Cost model (how much do you save vs. current system?)

4. **Recording pipeline fix** — Replace the 45-second sleep with a robust polling/retry mechanism. Recording should never silently fail.

5. **Dialler coupling** — How do you replace the binary circuit breaker with gradual backpressure? The dialler should slow down proportionally, not freeze entirely.

6. **Data model** — What new tables/columns do you need? How do you separate the source of truth (full analysis results) from the hot cache (dashboard reads)?

7. **Observability** — What do you log, what do you alert on, what dashboards do you build?

8. **Trade-offs and alternatives considered** — What did you consider and reject? Why?

### Part 2: Implementation

Implement your design. The scope is intentionally larger than what can be fully completed — we want to see how you prioritise.

**Must implement:**
- [ ] The triage/filtering layer that classifies calls before full LLM analysis
- [ ] Hot lane and cold lane routing logic
- [ ] Recording poller with retry/backoff (replacing `asyncio.sleep(45s)`)
- [ ] Data model changes (new tables, schema migration)
- [ ] Tests that validate your triage logic against the sample transcripts in `tests/fixtures/sample_transcripts.json`

**Should implement (if time permits):**
- [ ] Workflow orchestration (Temporal, or equivalent durable execution)
- [ ] Batch API integration for cold lane
- [ ] Quota tracking and gradual backpressure for dialler coupling
- [ ] Observability: structured logging, alert thresholds

**Nice to have:**
- [ ] Per-customer NLU configuration (different keyword rules per customer)
- [ ] CRM push with retry logic
- [ ] Dashboard update mechanism (hot cache writes)

---

## Constraints

These are non-negotiable requirements your solution must satisfy:

1. **The FastAPI endpoint interface does not change.** `POST /session/{sid}/interaction/{iid}/end` must continue to work with the same request/response schema. The endpoint must return 200 immediately (non-blocking).

2. **No analysis result may be permanently lost.** If a processing step fails, there must be a retry mechanism with visibility. Silent drops are unacceptable.

3. **The system must handle 100K calls per campaign run** without building a backlog that delays high-value calls by more than 15 minutes.

4. **LLM API costs must be reduced by at least 30%** compared to the current "full analysis on every call" approach. Show your cost model.

5. **Recording failures must be observable.** Every recording that fails to upload must produce a logged, alertable event — never a silent skip.

6. **Per-customer configurability.** Different customers have different definitions of "actionable" calls. The triage logic must be configurable per customer without code deployment.

7. **The solution must be testable locally** with `docker-compose up` (Postgres + Redis) and mock LLM responses. No real API keys required.

---

## Acceptance Criteria

Your submission is evaluated against these criteria:

| # | Criterion | How We Verify |
|---|-----------|--------------|
| AC1 | Triage filter correctly routes ≥90% of sample transcripts to the expected lane | Run your tests against `sample_transcripts.json` — hot calls → hot lane, cold calls → cold lane |
| AC2 | Triage configuration changeable per customer without deployment | Update config (DB or file), observe next call uses new config |
| AC3 | Hot lane end-to-end SLA ≤ 5 minutes (call end → result written) | Integration test or simulation showing p98 latency |
| AC4 | Cold lane uses a cheaper processing path (batch API or deferred) | Code inspection: cold lane calls are not processed via streaming/real-time LLM |
| AC5 | Recording poller retries with backoff; never silently skips | Unit test: simulate delayed recording, verify retry loop and failure logging |
| AC6 | Analysis results stored in append-only table (source of truth) separate from dashboard cache | Schema inspection: two separate storage locations with clear write sequence |
| AC7 | Post-call LLM backlog does not binary-freeze the dialler | Design doc explains gradual backpressure; no hardcoded 1800s freeze |
| AC8 | All failures produce structured, alertable log events | Code inspection: every error path emits a structured log with interaction_id |
| AC9 | Solution handles short transcripts (< 4 turns) without LLM cost | Test: short transcript → no LLM call, status updated directly |
| AC10 | Cost model shows ≥30% reduction vs. current approach | Design doc includes calculation with assumptions stated |

---

## Evaluation Criteria

We evaluate along four dimensions:

### 1. System Design (40%)
- Is the architecture sound? Does it handle the stated scale?
- Are failure modes addressed with real solutions, not hand-waving?
- Are trade-offs explicitly stated and reasoned about?

### 2. Code Quality (30%)
- Is the code clean, well-structured, and production-ready?
- Are the right abstractions chosen (not over-engineered, not under-engineered)?
- Error handling: does the code fail gracefully with visibility?

### 3. Prioritisation & Judgment (20%)
- Given limited time, did the candidate build the highest-impact pieces first?
- Are "should implement" items attempted only after "must implement" is solid?
- Does the candidate know when to stop and document vs. keep coding?

### 4. Communication (10%)
- Is the design document clear and concise?
- Can a team member understand the architecture from the doc alone?
- Are decisions explained, not just stated?

---

## Rules

- **You may use AI tools** (Copilot, ChatGPT, Claude, etc.). This is explicitly allowed. However:
  - You must be able to **explain every design decision** in your submission
  - Copy-pasting without understanding will be obvious in the follow-up discussion
  - AI-generated code that doesn't integrate with the existing codebase will be penalised

- **Time budget: 4–6 hours.** You will not finish everything. That's intentional. We're testing judgment as much as execution.

- **Submit via git.** Your final submission should be a clean git history showing your progression. Atomic commits with clear messages are valued.

---

## Getting Started

```bash
# 1. Clone and explore the current system
cd sde-assignment
cat src/tasks/celery_tasks.py    # Start here — this is the core problem

# 2. Read the sample transcripts
cat tests/fixtures/sample_transcripts.json

# 3. Start infrastructure (optional, for integration testing)
docker-compose up -d

# 4. Run existing tests
pip install -r requirements.txt
pytest tests/ -v

# 5. Start your design document
cp SUBMISSION_TEMPLATE.md SUBMISSION.md
# Edit SUBMISSION.md with your design

# 6. Implement your solution
# Modify existing files and add new ones as needed
```

---

## Questions?

If anything in the problem statement is ambiguous, **state your assumption in SUBMISSION.md and proceed.** In a real system design, you'd ask the team — here, reasonable assumptions are part of the evaluation.

Good luck.

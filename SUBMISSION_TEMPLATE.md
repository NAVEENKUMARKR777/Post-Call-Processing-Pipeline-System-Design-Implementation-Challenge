# Post-Call Processing Pipeline — Design Document

**Author:** [Your Name]
**Date:** [Date]

---

## 1. Assumptions

_State every assumption you made about the business, system, or environment. Be specific. These will be discussed in the follow-up._

1. ...
2. ...

---

## 2. Problem Diagnosis

_Before designing anything: what is actually broken, and why does it break at scale? In your own words._

---

## 3. Architecture Overview

_End-to-end flow from call-end webhook to completed analysis. Include a diagram._

```
[Your architecture diagram — ASCII or Mermaid]
```

### Key design decisions

1. ...
2. ...

---

## 4. Rate Limit Management

_This is the primary problem. How does your system respect LLM rate limits across 100K calls?_

### How you track rate limit usage

### How you decide what to process now vs. defer

### What happens when the limit is hit (recovery, not crash)

---

## 5. Per-Customer Token Budgeting

_If total capacity is N tokens/min and K customers are active simultaneously:_

- How do you allocate capacity across customers?
- What guarantees does a customer with a pre-allocated budget receive?
- What happens when a customer exceeds their budget?
- What happens to unallocated headroom?

---

## 6. Differentiated Processing

_Some call outcomes are time-sensitive. Some can wait. How do you determine which is which?_

_What mechanism do you use — is it a classification step, a flag set by the business, something else? Justify your choice._

---

## 7. Recording Pipeline

_Replacement for `asyncio.sleep(45s)`. How does it work? What does a failure look like to the on-call engineer?_

---

## 8. Reliability & Durability

_How do you ensure no analysis result is permanently lost?_

---

## 9. Auditability & Observability

_How would you debug a specific failed interaction 3 days after the fact?_

### What you log (and what fields every log event includes)

### Alert conditions

---

## 10. Data Model

_Schema changes required. Show the SQL._

```sql
-- Your schema additions/changes here
```

---

## 11. Security

_What data in this system is sensitive? How do you protect it at rest and in transit?_

---

## 12. API Interface

_Did you change the API contract (`POST /session/.../end`)? If yes, explain why. If no, explain why you kept it._

---

## 13. Trade-offs & Alternatives Considered

| Option | Why Considered | Why Rejected / What You Chose Instead |
|--------|---------------|--------------------------------------|
| ... | ... | ... |

---

## 14. Known Weaknesses

_What are the gaps in your design? What would you address next?_

---

## 15. What I Would Do With More Time

_Specific, prioritised list — not a generic wishlist._

1. ...
2. ...

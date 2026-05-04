# Post-Call Processing Pipeline — Design Document

**Author:** [Your Name]
**Date:** [Date]
**Time Spent:** [X hours]

---

## 1. Architecture Overview

_Describe the end-to-end flow from call-end webhook to completed analysis. Include a diagram._

```
[Your architecture diagram here — ASCII or Mermaid]
```

### Key Design Decisions

_List the 3-5 most important decisions and why you made them._

1. ...
2. ...

---

## 2. Triage / Filtering Strategy

_How do you classify calls before full LLM analysis? What's the classification approach?_

### Classification Stages

_Describe each stage of your filter._

### Configuration

_How is the filter configured per customer? Where does config live?_

---

## 3. Two-Lane Processing

### Hot Lane

- **Trigger:** ...
- **Processing:** ...
- **SLA target:** ...

### Cold Lane

- **Trigger:** ...
- **Processing:** ...
- **SLA target:** ...

### Cost Model

_Show the math. State your assumptions._

```
Current cost:    ...
Proposed cost:   ...
Savings:         ...
```

---

## 4. Recording Pipeline

_How does your recording poller work? What happens on failure?_

---

## 5. Dialler Coupling

_How do you replace the binary circuit breaker with gradual backpressure?_

---

## 6. Data Model

_What new tables/columns? Show the schema._

```sql
-- Your schema changes here
```

---

## 7. Observability

_What do you log? What do you alert on? What dashboards would you build?_

---

## 8. Trade-offs & Alternatives Considered

_What did you consider and reject? Why?_

| Option | Considered | Rejected Because |
|--------|-----------|-----------------|
| ... | ... | ... |

---

## 9. What I Would Do With More Time

_What's left unfinished? What would you prioritise next?_

---

## 10. Assumptions

_List any assumptions you made about the system._

1. ...
2. ...

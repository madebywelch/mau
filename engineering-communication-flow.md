# Engineering Team Communication Flow

A reference for how information should move through an engineering org — from product
initiatives down to individual contributors, and laterally between engineers whose
work depends on each other's. The goal is to reduce ambiguity, surface dependencies
early, and prevent the most common failure modes: late integration, silent blockers,
scope drift, and decisions that nobody can find later.

---

## Roles at a Glance

| Role                       | Primary responsibility                          | Communication outputs                       |
| -------------------------- | ----------------------------------------------- | ------------------------------------------- |
| Product Manager (PM)       | Defines *what* and *why*                        | PRDs, prioritization, success metrics       |
| Engineering Manager (EM)   | Translates *what* into *how much / when / who* | Scoped epics, task breakdown, capacity plan |
| Tech Lead / Staff Engineer | Defines *how* — architecture and contracts     | Tech specs, API contracts, ERDs             |
| Frontend Engineer          | UI, client state, UX implementation             | Components, frontend tasks, mocks           |
| Backend Engineer           | APIs, business logic, integrations              | Service code, API contracts                 |
| Database / Data Engineer   | Schema, migrations, query performance           | Migration PRs, schema docs                  |
| Designer                   | UX flows, visual design                         | Mocks, prototypes, design tokens            |
| QA / Test Engineer         | Verification, edge cases, automation            | Test plans, bug reports                     |
| DevOps / Platform          | Infra, CI/CD, observability                     | Pipelines, dashboards, runbooks             |

---

## High-Level Flow

```
                    ┌────────────────┐
                    │    Product     │   (the WHY)
                    └────────┬───────┘
                             │ Initiative / PRD
                             ▼
                    ┌────────────────┐
                    │  Eng Manager   │   (the WHO & WHEN)
                    └────────┬───────┘
                             │ Epic + breakdown
                             ▼
                    ┌────────────────┐
                    │   Tech Lead    │   (the HOW: contracts)
                    └──┬─────┬─────┬─┘
                       │     │     │
            ┌──────────▼┐  ┌─▼────┐ ┌▼──────────┐
            │ Frontend  │◀▶│Backend│◀▶│ Database │
            └───────────┘  └──────┘  └──────────┘
                  ▲           ▲           ▲
                  └─── lateral dependencies ───┘
                        ▲             ▲
                  ┌─────┴─────┐  ┌────┴────┐
                  │    QA     │  │ DevOps  │
                  └───────────┘  └─────────┘
```

Two flows are happening at once:

- **Vertical** (top-down): scope and ownership flow from product → EM → engineers.
- **Lateral** (peer-to-peer): contracts and dependencies flow between engineers
  building dependent pieces of the same feature.

Vertical comms usually have meeting rituals; lateral comms often don't, which is
why most "we shipped late" post-mortems trace back to lateral failures.

---

## Vertical Communication

### 1. Product → EM — the *what & why*

**Artifact:** PRD (Product Requirements Document)

A good PRD answers:

- **Problem.** Who is hurting, and why?
- **Success criteria.** What changes when this ships? How will we measure it?
- **Scope.** What's in / what's out for this iteration?
- **Constraints.** Deadlines, regulatory, budget, dependencies on other teams.
- **Open questions.** Things product needs eng input on before scoping.

> **Anti-pattern:** PM describes the *solution* ("build a dropdown that does X")
> instead of the *problem* ("users can't find Y"). EMs should push back and ask for
> the underlying need — solutions are engineering's job to propose.

### 2. EM → Engineers — the *how much, by whom, by when*

**Artifact:** Epic + tasks (Linear / Jira / GitHub Issues), capacity plan

The EM's job at this hand-off is:

1. **Decompose** the initiative into discrete work streams (frontend, backend, DB,
   infra, design, QA).
2. **Identify dependencies** between streams *before* assigning anything.
3. **Sequence** the work — what blocks what, what can run in parallel.
4. **Match tasks to people** based on skill, capacity, and growth opportunity.
5. **Define done** — acceptance criteria per task, not just per epic.

**Per-task structure:**

- Title (verb-first, scoped)
- Description (1–3 paragraphs of context, links to PRD + tech spec)
- Acceptance criteria (testable bullets)
- Dependencies (upstream / downstream tasks)
- Owner + reviewer
- Estimate / size

> **Anti-pattern:** Tasks like "Build the user profile page." A good task names
> the surface, the contract it consumes, and the criteria for done — otherwise
> the engineer has to re-derive scope from the PRD on their own.

---

## Lateral Communication

This is where most teams fail silently. The principle that prevents the failure:

> **Contract first.** When two engineers depend on each other, the *interface
> between them is the first deliverable*, not a side effect of one of them
> finishing first.

```
Without contract-first:                With contract-first:
─────────────────────────              ─────────────────────────
Backend builds API           ──▶       Tech lead drafts API spec        ──▶
Frontend waits               ──▶       FE + BE review & agree           ──▶
Frontend integrates last     ──▶       FE mocks, BE implements
Friction at integration                Both work in parallel; clean integration
```

### Frontend ↔ Backend

**Contract artifact:** OpenAPI spec, GraphQL schema, or typed client (tRPC, gRPC).

| What FE needs from BE       | What BE needs from FE                |
| --------------------------- | ------------------------------------ |
| Endpoint paths, methods     | Expected request shapes              |
| Request / response shapes   | UI states (loading, error, empty)    |
| Auth model                  | Pagination / filtering needs         |
| Error codes and meanings    | Performance budgets (latency, size)  |
| Pagination contract         | Realtime / streaming requirements    |

**Working pattern:**

1. PM and tech lead draft the feature; tech lead writes the API contract.
2. FE and BE engineers review the contract together (30-min sync or async PR).
3. BE implements against the contract; FE mocks against the same contract.
4. When BE merges a stub, FE swaps mock → real call.
5. Integration is a non-event because both sides built to the same spec.

### Backend ↔ Database

**Contract artifact:** Migration PR + schema doc / ERD.

| What BE needs from DB            | What DB needs from BE                |
| -------------------------------- | ------------------------------------ |
| Schema (tables, columns, types)  | Query patterns (read- vs write-heavy)|
| Indexes for known queries        | Volume estimates                     |
| Migration timing & rollback plan | Transaction boundaries               |
| Constraints (FK, unique, NOT NULL)| Retention / archival needs          |

**Working pattern:**

1. BE engineer proposes the schema change as a migration PR.
2. DB owner reviews for: indexing, naming, migration safety (locking, backfill
   strategy), retention.
3. Migrations land *before* the code that depends on them.
4. Both sides agree on the rollback path before deploying.

### Frontend ↔ Designer

**Contract artifact:** Figma file + design tokens + component spec.

- Designer hands off mocks; FE engineer asks "what's the empty state? loading?
  error? long-content overflow? what happens at 320px?" *before* starting.
- Design tokens (colors, spacing, typography) live in code as a shared library,
  not redefined per feature.

### Engineer ↔ QA

- QA is read in *during planning*, not at the end.
- The test plan lives next to the tech spec and is reviewed alongside it.
- Bugs are filed against acceptance criteria, not vibes.

### Engineer ↔ DevOps / Platform

- New runtime dependencies (queues, caches, third-party services) need platform
  sign-off *before* implementation, not at deploy time.
- Observability requirements (metrics, logs, traces, alerts) are defined in the
  tech spec, not bolted on after launch.

---

## Dependency Management

### Map dependencies up front

At the start of every epic, the EM or tech lead produces a **dependency map**:

```
DB schema (Alice) ──▶ Backend API (Bob) ──▶ Frontend integration (Carol)
                                            ▲
                                            │
                                  Designer mocks (Dana)  ◀── runs in parallel
```

Each arrow is a hand-off that needs three things:

- An agreed **contract** (schema, API spec, mock).
- A target **date**.
- A defined **"done" signal** (PR merged? Stub deployed to staging?).

### Unblocking patterns

| Situation                            | Pattern                                        |
| ------------------------------------ | ---------------------------------------------- |
| FE blocked on BE                     | Mock the API contract; swap in the real call later |
| BE blocked on DB schema              | Migration first, code second                   |
| Two teams blocked on each other      | Daily 15-min sync until unblocked, then drop it|
| One engineer is the bottleneck       | EM rebalances or pairs                         |
| External dependency unknown          | Time-boxed spike → decide → move on            |
| Contract genuinely can't be agreed   | Escalate to tech lead / staff for a call       |

### Integration checkpoints

For any feature with more than two contributors, schedule explicit milestones:

1. **Contracts agreed** — APIs, schemas, mocks ready.
2. **Stubs deployed** — every piece compiles and runs end-to-end with fakes.
3. **First slice working** — one happy path through the whole stack.
4. **Feature complete** — all acceptance criteria pass.
5. **Hardening** — perf, edge cases, observability, alerting.

If you can't point at the current checkpoint, you don't know where you are.

---

## Channels & Cadence

| Channel                  | Use for                                      | Don't use for                  |
| ------------------------ | -------------------------------------------- | ------------------------------ |
| **Sync meeting**         | Decisions with disagreement, kickoffs, retros| Status updates                 |
| **Async written update** | Status, agreed decisions, FYI                | Open-ended brainstorming       |
| **PR review**            | Code-level feedback, contract validation     | Architecture debates           |
| **Tech spec / RFC**      | Design decisions, trade-off analysis         | Tactical task tracking         |
| **Slack / chat**         | Quick questions, urgent unblocks             | Decisions (they get lost)      |
| **Issue tracker**        | Work items, acceptance criteria              | Long-form discussion           |
| **ADR**                  | "We decided X because Y" — durable record    | Day-to-day status              |

### Cadence

- **Daily.** 15-min standup — *blockers and asks only*, not status theater.
- **Weekly.** EM 1:1s with each engineer; team sync on epic progress.
- **Per epic.** Kickoff (contracts), midpoint (integration), retro (what to change).
- **Per quarter.** Roadmap review with product.

---

## Anti-patterns

| Anti-pattern              | What it looks like                                     | Fix                                                |
| ------------------------- | ------------------------------------------------------ | -------------------------------------------------- |
| **Telephone game**        | PM → EM → eng — details lost at each hop               | Engineers attend PRD review directly               |
| **Late integration**      | Each piece "done" but nothing fits together            | Integration milestones, contract-first             |
| **Hero engineer**         | One person knows everything, becomes the bottleneck    | Mandatory pairing, doc-as-you-go                   |
| **Silent blocker**        | Stuck for a day before mentioning it at standup        | Norm: "stuck >30 min = ask in channel"             |
| **Scope creep via DM**    | PM asks for "small additions" in chat                  | Route all scope changes through the issue tracker  |
| **Decisions in chat**     | Important calls made in Slack, never written down      | ADR for any non-trivial decision                   |
| **Status theater**        | Standup is everyone reading their commits aloud        | Status goes async; standup is blockers and asks    |
| **Mystery API**           | FE hits BE endpoints without a documented contract     | Contract is the first artifact, not the last       |
| **One-way handoff**       | "I'm done, it's your problem now"                      | Pair on integration; the team owns the feature     |

---

## Templates

### PRD (one-pager)

```markdown
# [Feature name]
**Author:** [PM]   **Status:** Draft / Approved   **Date:** YYYY-MM-DD

## Problem
Who is hurting and why?

## Goal & success metric
What changes when this ships? How do we measure it?

## Scope
- In: ...
- Out: ...

## Constraints
Deadlines, dependencies, regulatory.

## Open questions
- [ ] Question for engineering
- [ ] Question for design
```

### Task / Issue

```markdown
## Context
Link to PRD, tech spec.

## Acceptance criteria
- [ ] User can ...
- [ ] Error case X returns Y
- [ ] Telemetry event Z fires

## Dependencies
Blocked by: #123 (BE API)
Blocks: #456 (E2E test)

## Owner / Reviewer
Owner: @alice   Reviewer: @bob
```

### API Contract Review Checklist

- [ ] Endpoint paths follow team convention
- [ ] Request / response types are unambiguous
- [ ] Error codes and meanings documented
- [ ] Auth and authorization model clear
- [ ] Pagination, filtering, sorting defined
- [ ] Backwards compatibility considered
- [ ] FE engineer has reviewed and signed off
- [ ] BE engineer has reviewed and signed off

### ADR (Architecture Decision Record)

```markdown
# ADR-NNN: [Decision title]

## Context
What's the situation that requires a decision?

## Options considered
1. Option A — pros / cons
2. Option B — pros / cons

## Decision
We chose X because Y.

## Consequences
What changes? What do we now have to live with?
```

---

## TL;DR

1. **Vertical:** Product describes *problems*; EM decomposes into *work*; engineers
   *build*. Each layer adds detail without losing the layer above.
2. **Lateral:** Define the *contract* first. Engineers on dependent work should
   build in parallel against an agreed interface, not serially against each other.
3. **Dependencies:** Map them up front, set integration checkpoints, unblock with
   mocks and stubs.
4. **Channels:** Match the medium to the message — decisions in writing, blockers
   in chat, status async, debate in person.
5. **The silent killers:** Late integration, silent blockers, and decisions made
   in DMs. Almost every "we shipped late" story is one of these three.

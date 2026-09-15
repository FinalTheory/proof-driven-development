# You Can Draw a Google Docs Design in 10 Minutes. Can You Prove It?

> **Boxes and arrows are cheap. Correctness is not.**

Most system-design answers stop once the architecture diagram looks plausible:

```text
Client
  ↓
WebSocket Gateway
  ↓
Collaboration Service
  ↓
Database / Cache / Queue / Snapshot Store
```

That is enough to describe *where* data flows.

It is not enough to explain why the system is correct.

A production collaborative editor has to survive retry, concurrency, owner failover, stale processes, snapshotting, compaction, reconnect, ACK loss, replay, and partial failure without silently violating its user-visible guarantees. The difficult part is not drawing another box. The difficult part is making the correctness argument explicit enough that another engineer — or an AI agent — can challenge it, change the implementation, and know exactly what must be revalidated.

This repository is being built as an executable case study in **Proof-Driven Development**.

The current repository contains the architecture narrative, the machine-readable correctness DAG, and deterministic refinement/mutation tooling. Local mutation plans and audit scratch are intentionally excluded from version control; Git commits are the durable repository history. The collaborative-editor implementation, executable evidence layer, and deliberate semantic mutations are the next stage of the project.

The goal is not to prove every line of application code.

The goal is to make a small set of **high-impact architectural guarantees** explicit, reusable, and continuously checkable.

## Current status

The correctness model is live and runnable today. The implementation/evidence layer is still under construction. The current graph and its tooling live under [`correctness/`](correctness/); the source architecture is [`design/google-docs.md`](design/google-docs.md); agent/orchestrator rules are in [`AGENTS.md`](AGENTS.md).

```bash
cd correctness
python3 -m pip install -r requirements.txt
python3 correctness.py validate
python3 correctness.py refinement-status --format compact-yaml
```

---

## The central idea

Traditional development usually looks roughly like this:

```text
requirements
→ architecture
→ implementation
→ tests
→ code review
```

The correctness argument is mostly implicit. It lives in design discussions, reviewer memory, historical incidents, and whatever assumptions happened to survive implementation.

Proof-Driven Development turns that relationship around:

```text
workload + product requirements
        ↓
candidate architecture
        ↓
root correctness guarantees
        ↓
proof-obligation decomposition
        ↓
leaf contracts / assumptions
        ↓
implementation
        ↓
executable evidence
```

The architecture and the correctness argument evolve together.

A code change is therefore not reviewed only as a diff. It is also treated as a potential change to a proof dependency:

```text
implementation change
        ↓
affected correctness leaf
        ↓
transitive proof invalidation
        ↓
parent guarantees become stale
        ↓
required evidence is rerun or redesigned
```

This is similar to a build system, except the dependency graph does not describe which source files must be recompiled. It describes **which system guarantees must be re-established**.

---

## Why Google Docs?

A collaborative editor is a useful test case because the product is easy to understand while the correctness model is not.

At the UI level, the requirement sounds simple:

> Multiple users edit the same document and eventually see the same result.

But a real design quickly depends on properties such as:

- acknowledged edits must survive owner crash or failover;
- retrying one logical edit must not create a second canonical operation;
- accepted revisions must form one gap-free authoritative history;
- a stale owner must not append after ownership has moved;
- snapshot + replay must reconstruct the same observable document state as full history replay;
- reconnect must neither omit nor double-apply accepted revisions;
- snapshotting or compaction must not silently destroy idempotency or recovery evidence.

These are not implementation details. They are the properties that make the architecture *mean what we think it means*.

The repository models them explicitly.

---

## The system

The implementation uses a server-authoritative collaboration model.

A client submits a logical edit. The active document owner transforms/rebases it against committed history, then attempts to create a canonical accepted change. The authoritative persistence layer stores the accepted history, revision frontier, ownership epoch, snapshot state, and the identity needed for idempotent retry.

A simplified data flow looks like:

```text
Client raw edit
     ↓
Active OT owner
     ↓
transform against committed prefix
     ↓
authoritative acceptance transaction
     ├── accepted change
     ├── latest revision
     ├── idempotency identity
     └── owner epoch check
     ↓
commit
     ↓
ACK + live broadcast
```

Recovery reconstructs owner state from:

```text
published snapshot @ S
        +
canonical accepted tail S+1..H
        ↓
recovered state @ H
        ↓
resume acceptance at H+1
```

Reconnect uses either retained deltas or a snapshot-replacement response plus an exact canonical tail.

The interesting part of the project is not the presence of these components. It is the explicit argument for why their composition preserves the intended guarantees.

---

## The Correctness Assurance DAG

The repository represents correctness as a directed acyclic graph of claims.

Nodes are not services, classes, or files. A node is a proposition that can be challenged independently.

Edges mean **logical proof dependency**.

For example, one branch looks conceptually like this:

```text
G2: canonical history is linear and fenced
│
├── C7: revision frontier is linear
│   ├── initial history matches frontier
│   ├── append r requires pre.frontier = r-1
│   ├── accepted-change insert and frontier advance commit atomically
│   ├── committed append r leaves post.frontier = r
│   ├── append preserves the existing revision prefix
│   └── non-append lifecycle transitions preserve the revision domain
│
└── C8: stale owner is fenced
    ├── append requires current owner epoch
    └── authoritative epoch never regresses or reuses an old value
```

The important distinction is that these claims fail for different reasons.

For example:

```text
insert accepted_change@101
set latest_revision = 105
commit atomically
```

can satisfy the requirement that the row insert and frontier update occur in one transaction while still violating the stronger property that the committed frontier is exactly the revision that was appended.

That is why atomicity and frontier correctness are separate proof obligations.

The DAG makes that difference explicit.

---

## A performance optimization that breaks the proof

One of the most useful examples in this project came from an optimization that sounds completely reasonable in isolation.

Suppose `documents.latest_revision` is updated for every accepted edit. Someone notices that it is a per-document hot row and proposes:

> Let `accepted_changes` be the truth and update `latest_revision` asynchronously.

At the database level this sounds like a normal performance improvement.

At the system level it is not.

The current correctness argument uses the authoritative frontier as part of the serialization contract:

```text
append revision r may commit only if
pre.latest_revision == r - 1

and after commit
post.latest_revision == r
```

If `latest_revision` becomes eventually consistent, it can no longer play that role.

The change therefore invalidates the proof path for gap-free canonical history.

That does **not** mean the optimization is forbidden.

It means the engineer must provide a replacement mechanism that carries the same proof responsibility: an authoritative tail lookup, per-document lock, sequencer, transactional append primitive, or some other mechanism that proves every accepted append extends exactly the current canonical tail.

This is the difference between implementation replacement and semantic regression.

> **A performance optimization is not an optimization if it silently invalidates the correctness argument.**

---

## What changes during code review?

Without a correctness model, a review of the previous change might look like:

```text
remove synchronous latest_revision update
→ lower database contention
→ looks good
```

With the DAG:

```text
latest_revision semantics changed
        ↓
append-precondition obligation affected
append-postcondition obligation affected
        ↓
revision-frontier proof stale
        ↓
canonical-history guarantee requires revalidation
```

The reviewer no longer has to reconstruct the entire architecture from memory to realize that the field carries a system-level invariant.

This is the intended role of the tooling in this repository:

> **Ordinary AI review starts from a diff and guesses what may be dangerous. Proof-driven review starts from declared guarantees and asks which proof obligations the diff may have invalidated.**

---

## This project does not try to prove everything

A production application contains enormous amounts of domain-specific behavior:

```text
if customer_type == ...
if feature_flag == ...
if document_mode == ...
```

A missing branch may absolutely create a customer-visible bug.

That does not mean every product rule belongs in a formal correctness DAG.

Proof-Driven Development is most useful for a smaller class of properties that are:

- high blast-radius;
- relatively stable over time;
- architectural rather than feature-specific;
- sensitive to concurrency, retry, failure, recovery, or lifecycle behavior;
- expensive to rediscover during every review.

Typical examples include:

```text
idempotency
ordering
fencing
durability
atomic state transition
tenant isolation
snapshot/replay equivalence
migration safety
recovery invariants
```

The objective is not “prove the whole product correct.”

The objective is:

> **Make the small set of things that absolutely must not be accidentally broken explicit enough that the system can continuously defend them.**

---

## Adversarial refinement

The initial DAG is not assumed to be correct.

It is refined adversarially.

Each runnable claim is given to a fresh clean-context verifier that receives only a local proof slice:

```text
minimal system context
+ target proposition
+ explicit assumptions
+ direct proof obligations
+ failure model
```

The verifier first tries to construct a counterexample.

It may conclude that:

- the dependencies are sufficient;
- a premise is missing;
- a dependency is unrelated or stronger than necessary;
- a leaf actually bundles multiple independent correctness boundaries;
- the system specification itself is ambiguous and requires a human decision.

The clean verifier cannot mutate the graph.

A long-lived orchestrator acts as skeptical semantic judge and mutation compiler. It decides whether the counterexample is valid, whether the proposed decomposition is actually useful, whether an existing node already captures the property, and whether a suggested change would silently alter the architecture rather than refine its proof structure.

Only then can the DAG change.

This distinction is intentional:

```text
clean verifier
    = adversarial discovery

orchestrator
    = semantic judgment

deterministic tooling
    = mutation safety + scheduling + invalidation
```

---

## Proof reuse instead of repeated reasoning

Once a lower-level claim has been audited, parent claims treat it as a reusable contract.

For example, when proving:

```text
C10: catch-up represents exactly the missing suffix
```

an auditor does not reopen every snapshot and range-property proof underneath it. Stable direct dependencies become opaque proof boundaries.

This keeps proof composition modular:

```text
prove leaf once
      ↓
reuse proposition as contract
      ↓
only reopen it if its semantics change
```

Recursive semantic signatures make this mechanical. If a descendant proposition changes, dependent claims automatically become stale and re-enter the audit frontier.

---

## Executable evidence

A correctness claim is useful only if it eventually connects to something observable.

Different leaves use different verification mechanisms depending on the property:

```text
schema / uniqueness constraint
transaction contract test
state-machine test
property-based test
concurrency test
fault injection
replay / differential test
mutation test
runtime invariant
static analysis
LLM adversarial audit
human semantic decision
```

The project deliberately avoids pretending that every claim should receive the same kind of proof.

The engineering question is:

> **What is the cheapest verifier that provides enough assurance for this property?**

---

## Break it on purpose

The target executable demo will include deliberately incorrect implementation mutations that exercise the assurance system.

Examples include:

```text
async-latest-revision
broadcast-before-commit
remove-idempotency-evidence-after-compaction
allow-stale-owner-append
snapshot-frontier-mismatch
incomplete-reconnect-tail
```

Each mutation is designed to look locally plausible while violating a system-level property.

The intended end-state demo workflow is intentionally simple:

```bash
# Run the correct implementation
./pdd verify

# Inject a plausible but unsafe optimization
./pdd mutate async-latest-revision

# Recompute correctness impact and run affected evidence
./pdd verify
```

The output shows which proof obligations are affected and which root guarantees can no longer be trusted until new evidence is supplied.

This is the core demonstration the implementation phase is intended to deliver.

---

## Quick start

Today, the runnable artifact is the correctness model and refinement control plane:

```bash
git clone <repo-url>
cd proof-driven-development/correctness
python3 -m pip install -r requirements.txt

python3 correctness.py validate
python3 correctness.py refinement-status --format compact-yaml
python3 correctness.py catalog mechanisms
python3 correctness.py slice G1_retry_is_logically_exactly_once --format prompt
```

Run the control-plane regression suite after tooling changes:

```bash
python3 -m unittest -v test_correctness.py
```

The Dockerized collaborative editor, failure scenarios, evidence runner, and `pdd` wrapper shown elsewhere in this README describe the planned executable layer rather than functionality that is already shipped.

---

## Repository layout

```text
.
├── README.md
├── AGENTS.md
├── design/
│   └── google-docs.md
│
├── correctness/
│   ├── README.md
│   ├── prompts/
│   │   ├── README.md
│   │   ├── refinement.md
│   │   ├── coverage.md
│   │   └── composition.md
│   ├── correctness.yaml
│   ├── correctness.py
│   ├── graph_schema.py
│   ├── test_correctness.py
│   ├── requirements.txt
│   └── generated/
│
└── history/
    ├── README.md
    └── .gitignore
```

`history/` is local ignored scratch space for temporary mutation plans, audit notes, and diagnostics. Its runtime contents are not part of the repository contract and are not committed.

The implementation and verification directories will be added as the executable case study is built. The correctness model intentionally remains usable before that layer exists.

---

## Correctness cost should follow semantic blast radius

Proof-Driven Development is not intended to run an expensive full-system reasoning pass for every one-line change.

A practical review pipeline looks more like:

```text
PR diff
  ↓
deterministic implementation-surface mapping
  ↓
cheap high-recall relevance screening
  ↓
no correctness impact? ───────→ ordinary review
  ↓
possibly affected claims
  ↓
local proof invalidation
  ↓
expensive reasoning / evidence only where needed
```

Most changes should never invoke deep correctness reasoning.

The cost should scale with semantic risk, not repository size or diff size.

> **Correctness assurance cost should scale with semantic blast radius.**

---

## Why this matters for AI coding

AI makes implementation cheap.

It does not automatically make trustworthy implementation cheap.

As code-generation throughput increases, repeatedly asking a senior engineer to reconstruct every system invariant during every code review does not scale.

The higher-leverage alternative is to amortize that reasoning:

```text
senior / staff reasoning once
        ↓
explicit invariant / proof obligation
        ↓
reusable verification artifact
        ↓
future agents continuously reapply it
```

Human reasoning is still essential. Its highest-value role changes.

Instead of manually re-performing the same validation forever, engineers define:

- what the system actually guarantees;
- which assumptions are acceptable;
- which failure models matter;
- which tradeoffs require product or architecture decisions;
- what evidence is strong enough to trust a change.

Automation then helps preserve those decisions over time.

> **Senior engineers find correctness bugs. Staff engineers try to make the entire class of bugs harder to reintroduce.**

---

## What this project is trying to answer

This repository is also an experiment.

The interesting questions are not whether a DAG can be drawn or whether an LLM can generate long design documents. The real questions are economic and operational:

1. Can a non-trivial distributed system's correctness argument be decomposed into claims that remain understandable and stable over time?
2. Can code changes be mapped to affected correctness obligations with sufficiently high recall?
3. Can most leaf obligations eventually be discharged by mechanical evidence rather than repeated full-system reasoning?
4. Does the graph reduce reviewer cognitive load, or merely create a second specification that also becomes stale?
5. Can proof invalidation remain local enough that the cost of maintaining the model is lower than repeatedly rediscovering the same reasoning?
6. Can AI agents use an explicit correctness model to catch semantic regressions that ordinary diff-based review misses?

If the answers are mostly no, Proof-Driven Development is specification bureaucracy.

If the answers are mostly yes, correctness reasoning becomes a reusable engineering asset rather than a transient property of whoever happened to review the last PR.

---

## What this is not

This is not:

- a claim that ordinary application code can or should be formally proven;
- a replacement for tests;
- a replacement for code review;
- a replacement for canary deployment, observability, rollback, or incident response;
- an attempt to turn every business rule into a theorem;
- a claim that LLM judgments are authoritative.

It is an attempt to connect these pieces into a stronger chain:

```text
system guarantee
→ proof responsibility
→ implementation surface
→ executable evidence
→ change impact
→ revalidation
```

---

## The thesis

A ten-minute architecture diagram can show where requests go.

A serious system design must also explain what remains true when requests race, retries overlap, machines fail, ownership changes, snapshots replace history, and engineers optimize the implementation six months later.

That is the difference between a plausible architecture and an engineering argument.

> **You can draw a Google Docs design in 10 minutes. The interesting question is whether you can still explain why it works after the first failure, retry, optimization, and migration.**

That is what this repository is trying to make executable.

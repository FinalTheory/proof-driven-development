# You Can Draw a Google Docs Design in 10 Minutes. Can You Prove It?

> **Boxes and arrows on diagram are cheap. System that actually correctly built is expensive.**

[中文版本](README.zh-CN.md)

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

That is enough to explain where data flows. It is not enough to explain why the system remains correct when requests are retried, owners fail over, stale processes keep running, snapshots replace history, clients reconnect, ACKs disappear, and engineers start “optimizing” the implementation six months later.

This repository is an experiment in **Proof-Driven Development**: make a small set of high-impact architectural guarantees explicit, decompose them into independently reviewable proof obligations, and preserve that correctness reasoning as the system evolves.

The goal is not to formally prove every line of application code. The goal is to turn correctness reasoning that senior engineers normally reconstruct from memory into a reusable engineering asset.

---

## The core intuition: make the reasoning problem smaller

Suppose you open a fresh AI agent and ask only one question:

> Prove `P = NP`, or prove `P != NP`.

We cannot expect any agent in the foreseeable future to solve that correctly. The prompt is one sentence, but the reasoning space behind it is enormous.

Now ask:

> What is `1 + 1`?

It would be surprising for a sufficiently capable model to get that wrong.

The prompts are equally short. The essential difference is the **size and ambiguity of the reasoning task**.

Real distributed-system correctness lives somewhere between those extremes. For example:

> Is this collaborative editor correct under retry, failover, reconnect, snapshotting, history compaction, and concurrent ownership changes?

That question is too large to trust as one monolithic LLM judgment. Even a very strong model has to reconstruct too many hidden assumptions, cover too many failure paths, and maintain too many state and semantic relationships in one context.

Formal methods attack this by defining semantics precisely enough for a proof system or model checker. They can provide much stronger guarantees, but the main cost is often not “running the proof.” It is defining the formal state space, transition system, abstraction boundaries, and their correspondence to the real implementation. For an ordinary production system, that modeling cost is usually too high to pay everywhere.

Proof-Driven Development explores a middle ground:

> **Do not ask one agent to reason about the whole system at once. Keep decomposing the correctness argument until each local proof obligation is narrow, explicit, adversarially checkable, and difficult for a strong verifier to misunderstand.**

No local audit is assumed to be infallible. The bet is statistical and economic: when each obligation has precise semantics and a small context, independent strong auditors are much less likely to make the same reasoning mistake than when they are all asked to judge one giant architecture prompt.

The Harness exists to make that decomposition, context isolation, proof reuse, and revalidation systematic.

---

## From architecture to proof obligations

Traditional development often looks roughly like this:

```text
requirements
→ system design
→ architecture spec
→ implementation
→ tests
→ code review
```

The correctness argument is usually implicit. It lives in design discussions, reviewer memory, old incidents, and assumptions that happened to survive implementation.

Proof-Driven Development makes that chain explicit:

```text
workload + product requirements
        ↓
 candidate architecture
        ↓
 root correctness guarantees
        ↓
 proof-obligation decomposition
        ↓
 local proof boundaries / assumptions
        ↓
 implementation
        ↓
 executable evidence
```

A node in the correctness graph is a **proposition**, not a service, class, file, or box in an architecture diagram. An edge means logical proof dependency: if a lower-level proposition is no longer trusted, every guarantee that depends on it must be reconsidered.

That makes the correctness model resemble a build dependency graph:

```text
semantic change
      ↓
affected proof obligation
      ↓
transitive invalidation
      ↓
re-establish only the affected guarantees
```

A compiler rebuilds artifacts after source dependencies change. The Harness tries to do something analogous for correctness reasoning.

The detailed Harness semantics and type system live in [`correctness/SPEC.md`](correctness/SPEC.md); the current machine-readable model lives in [`correctness/correctness.yaml`](correctness/correctness.yaml).

---

## Why Google Docs?

A collaborative editor is a useful case study because the product is intuitive while the correctness argument is not.

At the UI level, the requirement sounds simple:

> Multiple users edit the same document and eventually see the same result.

But the architecture quickly depends on harder properties:

- an acknowledged edit must survive owner crash or failover;
- retrying one logical edit must not create a second canonical operation;
- accepted revisions must form one gap-free authoritative history;
- after ownership has moved, the stale owner must not continue appending;
- snapshot plus replay must reconstruct the same observable state as authoritative history;
- reconnect must neither omit nor double-apply accepted changes;
- lifecycle operations must not silently destroy the identity or provenance needed for retry and recovery.

These are not merely implementation details. They determine whether the architecture is actually the architecture we think it is.

---

## A locally reasonable optimization can break a global proof

Consider a simplified example.

Suppose `documents.latest_revision` is updated synchronously for every accepted edit. Someone notices that it has become a per-document hot row and proposes:

> Let `accepted_changes` be the source of truth and update `latest_revision` asynchronously.

At the database level, this sounds like an ordinary performance optimization.

But suppose the current correctness argument uses the revision frontier as part of the serialization contract:

```text
append revision r may commit only if
pre.latest_revision == r - 1

and after commit
post.latest_revision == r
```

If `latest_revision` becomes eventually consistent, it can no longer carry that proof responsibility.

That does not automatically forbid the optimization. The engineer can replace it with an authoritative tail lookup, sequencer, lock, transactional append primitive, or some other mechanism that proves the same property.

The important distinction is:

> **Replacing an implementation is fine. Silently deleting the proof responsibility it carried is not.**

Without an explicit correctness model, code review might stop at:

```text
remove synchronous frontier update
→ reduce contention
→ looks good
```

With a proof graph:

```text
frontier semantics changed
        ↓
serialization proof obligations affected
        ↓
dependent guarantees become stale
        ↓
replacement reasoning or evidence required
```

This is the practical value of the proof graph: the reviewer no longer has to reconstruct the whole architecture from memory before realizing that a seemingly local field carries a system-level invariant.

---

## The Harness is not trying to make LLMs authoritative

The initial decomposition is not assumed to be correct. It must be attacked continuously and adversarially.

A local verifier receives only the context required to judge the current proposition: the target, its direct premises or local proof boundary, relevant system semantics, and the failure model. Its first job is to construct a counterexample, not to produce a persuasive explanation.

That process can expose several different defects:

```text
missing premise
wrong dependency
one claim bundles multiple independent obligations
missing lifecycle transition
incorrect provenance assumption
genuine ambiguity in the system semantics
```

Context isolation does not mean “a short prompt magically becomes a proof.” It means each verifier faces a narrower problem with less room to smuggle in unstated assumptions.

The Harness then surrounds the uncertain reasoning step with machinery that is as deterministic as practical:

```text
clean-context local audit
        ↓
semantic judgment
        ↓
typed mutation
        ↓
validation
        ↓
signature-based invalidation propagation
        ↓
next local audit frontier
```

Lower-level propositions that have already been audited can be reused as opaque contracts instead of reopening the entire subtree every time a parent is examined. If the lower-level semantics change, recursive signatures automatically reopen the affected reasoning.

This is where the methodology tries to improve reliability without paying the full modeling cost of formal verification: **keep human/LLM reasoning as local as possible, and make the control machinery around that reasoning as deterministic as possible.**

---

## Proof obligations eventually have to touch the real implementation

A beautifully decomposed DAG is still only a specification artifact if its leaves are not connected to implementation evidence.

Different properties deserve different verification mechanisms. A leaf might ultimately be checked by:

```text
schema / uniqueness constraint
transaction contract test
control-flow / static-dataflow analysis
state-machine / property-based test
concurrency test
fault injection
replay / differential test
runtime invariant
model checker
```

The engineering question is not “How do we formally prove everything?” It is:

> **For this particular proof obligation, what is the cheapest independent verifier that still provides enough assurance?**

That distinction matters. A database uniqueness property should not require an LLM to reread the whole service. A crash-durability claim should not be considered proven merely because the code says it flushes. Evidence should observe the real property through an independent and economical boundary whenever possible.

---

## This project deliberately does not model everything

Production systems contain enormous amounts of feature-specific behavior. Many bugs matter to users without belonging in an architectural correctness DAG.

Proof-Driven Development is aimed at properties that are repeatedly depended on over time and are easy for later changes to break accidentally:

```text
idempotency
ordering
fencing
durability
atomic state transitions
state invariants
provenance bindings
snapshot / replay equivalence
recovery / migration safety
```

The useful boundary is not “important bug versus unimportant bug.” It is closer to:

> **Is this a stable, high-blast-radius correctness responsibility that many future changes must keep preserving?**

If not, ordinary tests and code review may be cheaper.

---

## Correctness cost should follow blast radius

A proof system that reruns expensive whole-system reasoning for every one-line change is economically useless.

The target workflow must remain local:

```text
implementation change
      ↓
map to affected implementation surfaces
      ↓
no relevant correctness obligation? ───→ ordinary review
      ↓
affected proof obligations
      ↓
local invalidation
      ↓
reasoning / evidence only where needed
```

Most code changes should never trigger deep correctness work.

Maintenance cost should scale with **blast radius**, not repository size or diff size.

That locality is one of the central hypotheses this project is testing. If every change eventually invalidates the entire graph, the methodology has failed operationally even if the model is theoretically correct.

---

## Why this matters more when AI writes the code

AI makes implementation cheaper. It does not automatically make trustworthy implementation cheaper.

As code-generation throughput rises, a new bottleneck becomes visible: humans still have to decide whether the generated system preserves the right semantics.

If every code review requires a senior engineer to reconstruct distributed invariants from scratch, that process does not scale. The higher-leverage alternative is to amortize the reasoning cost:

```text
senior / staff reasoning once
        ↓
explicit guarantee / proof obligation
        ↓
reusable verification boundary
        ↓
future humans and agents reuse it
```

Human judgment remains essential, but its role changes. Engineers decide what the system actually promises, which assumptions are acceptable, which failure models must be covered, and what evidence is strong enough to trust. Automation helps preserve those decisions instead of rediscovering them every time.

> **Senior engineers find correctness bugs. Staff engineers try to make the entire class of bugs harder to reintroduce.**

---

## What this project is actually testing

This repository is an experiment, not a claim that the methodology already works universally.

The hard questions are economic and operational:

1. Can a non-trivial distributed system be decomposed into proof obligations that remain understandable over time?
2. Can local proof obligations become precise enough that multiple independent AI auditors are sufficiently reliable on them?
3. Can specification gaps and bad decompositions be discovered without repeatedly reopening the entire system context?
4. Can implementation changes be mapped to affected proof obligations with sufficiently high recall?
5. Can enough leaf obligations ultimately be discharged by mechanical evidence that the model reduces, rather than increases, human review cost?
6. Can invalidation propagation remain local enough for the methodology to survive real system evolution?

If the answers are mostly no, Proof-Driven Development becomes specification bureaucracy.

If the answers are mostly yes, correctness reasoning can become a reusable and maintainable engineering asset instead of something that existed briefly in the previous reviewer’s head.

---

## What this is not

This is not a claim that ordinary application code should all be formally proven, that tests or code review become unnecessary, or that LLM judgments are authoritative.

It is an attempt to connect several existing practices into a stronger chain:

```text
system guarantee
→ proof responsibility
→ local reasoning boundary
→ implementation surface
→ independent evidence
→ change impact
→ targeted revalidation
```

Formal methods remain the right tool when the risk and economics justify them. Ordinary tests remain the right tool for most ordinary behavior. Proof-Driven Development explores the space in between.

---

## Explore the repository

The repository deliberately separates the target-system design from the Correctness Harness:

- [`design/google-docs.md`](design/google-docs.md) — the collaborative-editor architecture being analyzed;
- [`correctness/SPEC.md`](correctness/SPEC.md) — the human-readable mental model for the Correctness Harness;
- [`correctness/SCHEMA.md`](correctness/SCHEMA.md) — the canonical model field/type reference;
- [`correctness/correctness.yaml`](correctness/correctness.yaml) — the current machine-readable correctness model;
- [`correctness/correctness.py`](correctness/correctness.py) — validation, slicing, scheduling, semantic signatures, assurance state, and controlled mutation;
- [`AGENTS.md`](AGENTS.md) — repository operating rules for agents.

---

## Conclusion

A ten-minute architecture diagram can show where requests go.

A serious system design must also explain what remains true when requests race, retries overlap, machines fail, ownership changes, snapshots replace history, and future engineers start optimizing the implementation.

The core insight of this project is that we do not need one human or AI to hold the entire proof in its head at once.

If we can keep decomposing the argument into small enough proof obligations, give each verifier exactly the context it needs, attack those obligations from multiple independent perspectives, connect the leaves to real implementation evidence, and deterministically reopen the right reasoning when semantics change, then trustworthy correctness reasoning may become much cheaper to maintain over time.

> **Do not ask one agent to prove the whole system correct. Build a proof system that keeps giving AI questions small enough that errors become hard to hide.**

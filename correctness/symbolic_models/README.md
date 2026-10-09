# Symbolic assurance layer

The symbolic layer is a mechanically checkable projection of the canonical correctness model. It is **not** an independent correctness authority: `correctness.yaml` remains the source of system propositions, typed vocabulary, proof structure, formalization routing, and assurance state; the canonical design article remains the source for design semantics referenced by Design Obligations.

## Models vs artifacts

`symbolic_models/` contains maintained **solver inputs / formal specification sidecars**:

- `bridge.yaml` — typed symbolic lowering for canonical claims, assumptions, and semantic contracts.
- `assurance.yaml` — translation-assurance records binding one exact natural-language subject signature to one exact symbolic lowering.
- `coverage.yaml` — declarative adversarial bad-state cases used by the symbolic coverage regression harness.
- `design_obligations.yaml` — independently extracted required design properties used as the specification-coverage oracle above the correctness DAG.

Machine-check artifacts are reproducible **build outputs**, not maintained repository state. When explicitly requested, commands write them under ignored `../../temp/symbolic_artifacts/`; validation and reports normally replay the solver directly from the current models instead of reading a persisted proof file. Generated artifacts may still use closed schemas for interchange/debugging, but deleting them must never remove semantic authority or certification inputs.

The separation is therefore conceptual rather than a pair of tracked source directories: models answer **what proposition/query should be checked**; temporary artifacts answer **what one concrete solver run produced for debugging or external audit**.

## Closed schema boundary

Every persisted YAML file in `symbolic_models/` must belong to a registered closed JSON Schema exposed by `correctness.py formal-schema`. `correctness.py validate` rejects unknown YAML filenames in that maintained model namespace, and every object shape in the registered formal schemas closes or explicitly types `additionalProperties`. Generated temporary artifact payloads also have schemas when such an output format exists, but `temp/` is ignored and is never a source-of-truth namespace. Adding a new maintained formal YAML kind therefore requires adding its schema and repository validation first; free-form YAML metadata is not accepted.

The bridge is closed and typed. Every symbolic function has an explicit meaning and canonical provenance. A function is anchored either through `catalog_symbols` or, for an opaque relation that cannot be represented as a direct catalog projection, through `semantic_anchors` naming the canonical contract/claim/assumption/source whose semantics authorize that relation. An opaque function may only be used by an anchored subject; this prevents hidden glue or background premises from being introduced merely to obtain UNSAT.

Canonical state may declare `symbolic_dimensions`. Those coordinates resolve through `catalog.symbolic_dimensions`, and a symbolic function directly reading that state must preserve the declared arity/coordinate shape.

For `all_roots` Design Obligation checks, the authoritative solver boundary contains exactly every current root proposition plus every current assumption. A root whose symbolic mapping declares `formula_from_contracts: true` is materialized from the current formulas of its canonical `semantic_contracts` (plus any explicit residual formula) at check time. The referenced contract formulas are therefore part of the root's translation, not additional independent premises; mutating one contract automatically changes every root that derives from it without duplicating or double-asserting that contract.

A symbolic mapping also declares solver capability. `relational` mappings expose shared symbolic relations and may participate in authoritative theorem proving once translation assurance is `TRUSTED`. `opaque` mappings preserve a subject as a whole uninterpreted proposition for diagnostic boundary completeness, but they are never `SYMBOLIC_READY` and never count as trusted coverage/composition constraints; this prevents a faithful-but-unstructured translation from being mistaken for usable proof structure. Capability is proof metadata, not proposition semantics, so changing it does not invalidate an otherwise identical natural-language↔formula translation review.

A relational symbolic mapping is usable as trusted evidence only when its translation-assurance record is current and evaluates to `TRUSTED`. Provenance metadata such as `semantic_anchors` constrains where a relation may be used but does not itself change the translated proposition, so it is deliberately excluded from semantic translation signatures.

`correctness.py validate` reloads the formal models, validates bridge/canonical correspondence and predicate provenance, validates every persisted formal YAML against its registered schema, reruns trusted symbolic coverage and machine-checked compositions, and checks persisted artifacts against the current model. Formal-layer drift therefore fails repository preflight before a new certification can be claimed.

The generic symbolic kernel lives in `../harness/symbolic/` and contains no Google Docs-specific sorts or predicates. Formalization is progressive: a subject declared `mode: symbolic` may still be `MAPPING_MISSING`, `TRANSLATION_UNVERIFIED`, or `TRANSLATION_STALE`; those states are explicit formalization debt and never silently fall back to a different proof backend.

# Symbolic assurance sidecars

These files are a formal assurance layer over the canonical correctness model. They are **not** independent correctness authority: `correctness.yaml` remains the source of system propositions, typed vocabulary, proof structure, and assurance state. The sidecars have a different lifecycle because they record how selected canonical semantics are lowered and checked mechanically.

- `bridge.yaml` — typed symbolic lowering for selected canonical contracts and claims.
- `assurance.yaml` — persisted translation-assurance judgments that bind a natural-language subject to an exact symbolic lowering.
- `coverage.yaml` — declarative adversarial bad-state queries and their selected trusted symbolic constraints.
- `../symbolic_artifacts/*.yaml` — persisted machine-check artifacts produced from trusted translations.

The bridge is closed and typed. Every symbolic function must have an explicit meaning and canonical provenance. A function is anchored either through `catalog_symbols` or, for an opaque relation that cannot be represented as a direct catalog projection, through `semantic_anchors` naming the canonical contract/claim whose semantics authorize that relation. An opaque function may only be used by an anchored subject; this prevents hidden glue or background premises from being introduced merely to obtain UNSAT.

Canonical state may declare `symbolic_dimensions`. Those coordinates resolve through `catalog.symbolic_dimensions`, and a symbolic function directly reading that state must preserve the declared arity/coordinate shape.

A symbolic mapping is usable as trusted evidence only when its translation-assurance record is current and evaluates to `TRUSTED`. Provenance metadata such as `semantic_anchors` constrains where a relation may be used but does not itself change the translated proposition, so it is deliberately excluded from semantic translation signatures.

`correctness.py validate` validates this formal layer whenever these sidecars are present. It reloads all three sidecars, validates bridge/canonical correspondence and predicate provenance, reruns trusted coverage and machine-checked compositions, and checks persisted artifacts against the current model. A formal-layer drift therefore makes the repository-level preflight invalid before refinement or coverage campaigns start.

The generic symbolic kernel lives in `../harness/symbolic/` and contains no Google Docs-specific sorts or predicates. Formalization remains intentionally lazy: a canonical contract/claim may be unmapped, but anything recorded as trusted or machine-checked must pass the full correspondence and artifact checks.

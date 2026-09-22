# Experimental symbolic models

These files are **not** canonical correctness authority. They are POC sidecars used to test whether selected natural-language semantic contracts can be given a generic typed relational skeleton and checked with SMT.

`google_docs.poc.yaml` currently maps three real contracts from `correctness.yaml`:

- `client_canonical_frontier_coherence`
- `visible_canonical_speculative_exclusion`
- `request_authoring_frontier_binding`

The generic symbolic kernel lives in `../harness/symbolic/` and contains no Google Docs-specific sorts or predicates.

A mapping is accepted only when its contract class, observation scope where applicable, and catalog-symbol coverage match the canonical contract. The formula itself is still an explicit semantic translation; the Harness does not infer it from prose.

This sidecar exists specifically to avoid invalidating the canonical DAG while the symbolic representation is still experimental. Promotion into the canonical schema should happen only after the representation and correspondence rules have stabilized.

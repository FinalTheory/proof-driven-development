# Experimental symbolic models

These files are **not** canonical correctness authority. They are POC sidecars used to test whether selected natural-language semantic contracts can be given a generic typed relational skeleton and checked with SMT.

`google_docs.poc.yaml` currently maps nine real contracts from `correctness.yaml`:

- `client_canonical_frontier_coherence`
- `visible_canonical_speculative_exclusion`
- `request_authoring_frontier_binding`
- `catchup_head_binding`
- `accepted_evidence_correspondence`
- `accepted_evidence_provenance`
- `acceptance_request_identity_binding`
- `live_delivery_acceptance_binding`
- `recovery_head_binding`

It also contains three experimental claim mappings:

- `L92_captured_recovery_head_equals_authoritative_frontier`
- `L108_acceptance_preserves_request_identity`
- `L116_live_delivery_is_bound_to_committed_acceptance`

Each claim has its own explicit symbolic formula while reusing the typed predicates/vocabulary established by its referenced `semantic_contracts`. The bridge requires that the referenced contract set exactly match the canonical claim's declared `semantic_contracts`, and that the claim formula cover exactly the same catalog-symbol boundary. Translation assurance then checks whether the claim formula is actually faithful to the claim statement.

`assurance.poc.yaml` stores only normalized, completed translation-assurance records. Each record is pinned to a semantic signature of both the canonical source context and the exact symbolic mapping. A record becomes stale automatically when either side changes. Presence in `google_docs.poc.yaml` therefore means “formalized enough to experiment with”; only a matching `TRUSTED` assurance record means “eligible for authoritative symbolic checks.”

The generic symbolic kernel lives in `../harness/symbolic/` and contains no Google Docs-specific sorts or predicates.

A mapping is accepted only when its contract class, observation scope where applicable, and catalog-symbol coverage match the canonical contract. The formula itself is still an explicit semantic translation; the Harness does not infer it from prose.

This sidecar exists specifically to avoid invalidating the canonical DAG while the symbolic representation is still experimental. Promotion into the canonical schema should happen only after the representation and correspondence rules have stabilized.

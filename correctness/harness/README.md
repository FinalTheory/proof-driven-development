# Correctness Harness internals

This package is the internal implementation of the PDD correctness Harness. The public operational entry point remains `../correctness.py`; splitting code here must not change the canonical model or CLI semantics.

Current module boundaries:

- `contracts.py` — tool-level schemas, workflow/audit contracts, and Harness constants.
- `model.py` — YAML loading, graph model, catalog lookup, and plain-data normalization.
- `signatures.py` — proposition, composition, and model semantic signatures.
- `workflow.py` — refinement and assurance snapshots / scheduling state.
- `validation.py` — canonical graph validation and SCHEMA.md structural-reference validation.
- `symbolic/` — experimental generic typed symbolic IR and SMT backend.

The dependency direction is intentionally one-way:

```text
contracts
   ↓
model
   ↓
signatures
   ↓
workflow
   ↓
validation / higher-level CLI orchestration
```

Higher-level mutation, prompt generation, and CLI code still lives in `correctness.py` for now. Future extraction should preserve this direction rather than introduce circular imports.

## Symbolic POC boundary

The symbolic package is not yet canonical correctness authority. It experiments with a narrow idea:

> formalize identity, scope, binding, quantification, and relational safety structure while leaving domain meaning in natural-language/domain vocabulary.

The kernel contains no Google Docs-specific concepts. A program declares uninterpreted sorts and typed functions, supplies structured formulas, then asks whether `constraints AND bad_state` is satisfiable.

Interpretation:

- `SAT` — the symbolic specification admits the proposed bad state.
- `UNSAT` — the symbolic specification excludes that bad state within the modeled abstraction.
- Neither result proves that the symbolic abstraction faithfully captures the product design; that remains a semantic-authority question.

The POC deliberately does not model full transition systems, implementation correspondence, liveness, or arbitrary first-order logic.


### Semantic-contract bridge

`symbolic/bridge.py` connects selected canonical `semantic_contracts` to this IR without changing the canonical graph schema yet. Domain-specific mappings live outside the generic kernel, currently in `../symbolic_models/google_docs.poc.yaml`.

The bridge deliberately does **not** infer formulas from English. Each mapping supplies an explicit structured formula, while deterministic checks prevent silent drift:

- mapped contract class must equal the canonical contract class;
- a `state_invariant` mapping must preserve the canonical `observation_scope`;
- functions used by the formula declare which canonical catalog symbols they represent;
- the union of used catalog symbols must exactly cover the contract's `state_symbols` or `source_symbols + bound_symbols`.

This means the bridge can reject a structurally incomplete or over-scoped translation, but it still cannot prove that an opaque predicate such as `canonical_content_matches` faithfully captures the English definition. That semantic correspondence remains a human/LLM judgment boundary.


### Stable verification facade

`symbolic/verification.py` provides the current high-level interface:

- `SymbolicVerifier.check(query, contracts=[...])` asks whether the selected canonical contract mappings admit a candidate execution.
- `SymbolicVerifier.exclusion_check(query, contracts=[...])` compares the candidate without those contracts against the candidate with them. `closes_counterexample` is true only for the intended `SAT -> UNSAT` transition.

Contract applicability must be explicit in the structured formula. A guarantee scoped to an eligible request, eligible response, bounded retention horizon, or other semantic precondition must encode that condition as the implication guard. Tests should also assert that a corresponding out-of-scope violation remains satisfiable when the canonical contract intentionally makes no guarantee there. This prevents the SMT mapping from silently strengthening the natural-language specification.

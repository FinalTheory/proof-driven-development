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
- `SymbolicVerifier.check_claims(query, claims=[...])` and `claim_exclusion_check(...)` provide the same interface for explicitly formalized DAG claims rather than reusable semantic contracts.

Contract applicability must be explicit in the structured formula. A guarantee scoped to an eligible request, eligible response, bounded retention horizon, or other semantic precondition must encode that condition as the implication guard. Tests should also assert that a corresponding out-of-scope violation remains satisfiable when the canonical contract intentionally makes no guarantee there. This prevents the SMT mapping from silently strengthening the natural-language specification.


### Translation assurance

`symbolic/translation_assurance.py` treats natural-language-to-symbolic lowering as a probabilistic semantic compilation step surrounded by deterministic checks.

For one mapping, the intended fresh-agent gate is:

1. one blind round-trip agent sees only typed predicate meanings, catalog vocabulary, Harness contract-class semantics, and the formula, then renders the symbolic proposition back into natural language;
2. two direct reviewers compare the original proposition with the symbolic formula, one biased toward finding weakening/omission and one toward strengthening/scope expansion;
3. two round-trip reviewers compare the original proposition only with the blind natural-language rendering, again with complementary weakening/strengthening attack directions.

The default aggregation rule requires at least two direct and two round-trip `EQUIVALENT` verdicts. Any concrete `WEAKER`, `STRONGER`, or `MISMATCH` verdict rejects the mapping. Reviewer execution/format failures or unresolved ambiguity produce `INCOMPLETE`, never a trusted result.

Predicate declarations include explicit natural-language `meaning`; identifiers alone are not semantic authority. Review context also includes generic Harness semantics such as: a `state_invariant` holds at every state in its `observation_scope`, while a `provenance_binding` does not imply liveness or ordering unless explicitly encoded.

Experimental claim mappings reuse the typed predicates/vocabulary of already-formalized semantic contracts but carry their own explicit claim formula. Deterministic validation requires a claim mapping to reference exactly the canonical claim's declared `semantic_contracts` and to cover the same catalog-symbol boundary; the same translation-assurance reviewers then determine whether the claim formula is actually equivalent to the claim statement. This allows a narrow leaf claim to project only the part of a broader semantic contract that it actually states.


### Persisted trust and invalidation

`symbolic/assurance_registry.py` persists normalized translation-review results against a SHA-256 signature of the exact semantic source context and symbolic translation under review. The signature includes canonical proposition text, referenced canonical vocabulary/contract context, typed predicate meanings, and the structured formula.

A review record therefore has four meaningful states:

- `TRUSTED` — current source/mapping signature matches and all required direct + round-trip reviewers judged the translation equivalent;
- `REJECTED` / `INCOMPLETE` — semantic disagreement, ambiguity, reviewer failure, or insufficient independent reviews;
- `STALE` — a previously reviewed source or symbolic mapping changed;
- `UNVERIFIED` — no review record exists.

Only `TRUSTED` mappings are eligible for authoritative symbolic proof use. Ordinary `check()` / `exclusion_check()` remain available as experimental diagnostics; `trusted_check()`, `trusted_exclusion_check()`, and their claim equivalents enforce the registry gate.

### Coverage-facing CLI

The public Harness entry point exposes the experimental symbolic layer without requiring agents to write Python:

- `correctness.py symbolic-status` reports the trust frontier for every mapped contract and claim;
- `correctness.py symbolic-check --kind contract|claim --subject ... --query bad-state.yaml` compares the candidate bad state with and without the selected mapping and, by default, refuses any mapping that is not currently `TRUSTED`;
- `--allow-untrusted` exists only for POC/debug work and must not be treated as authoritative coverage evidence.

This makes the intended coverage integration explicit: LLM agents still propose semantically meaningful bad states, while deterministic SMT checks answer whether trusted symbolic specifications already exclude them.

# Correctness Harness internals

This package is the internal implementation of the PDD correctness Harness. The public operational entry point remains `../correctness.py`; splitting code here must not change the canonical model or CLI semantics.

Current module boundaries:

- `contracts.py` — tool-level schemas, workflow/audit contracts, and Harness constants.
- `model.py` — YAML loading, graph model, catalog lookup, and plain-data normalization.
- `signatures.py` — proposition, composition, and model semantic signatures.
- `workflow.py` — refinement and assurance snapshots / scheduling state.
- `validation.py` — canonical graph validation and SCHEMA.md structural-reference validation.
- `symbolic/` — generic typed symbolic IR, translation assurance, SMT backend, and persisted formal-layer validation.

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

## Semantic-contract realization boundary

A claim's `semantic_contracts` are not descriptive tags. During refinement, each reference creates a mandatory realization obligation: the authoritative claim must entail the reusable contract's complete guarantee across its declared semantic scope. This entailment remains a natural-language semantic judgment, but the Harness makes the judgment explicit and fail-closed: `audit-prompt` requires a per-contract `REALIZES / WEAKER / MISMATCH / INCOMPLETE` result, and `set_refinement(status=stable)` must declare exactly the current contract IDs judged `REALIZES`. A dedicated realization-semantics signature version invalidates affected proof branches when this audit contract changes.

Generated proof slices close every referenced semantic contract over its typed vocabulary. Structured `state_symbols`, `source_symbols`, and `bound_symbols` are injected from `catalog.terms` / `catalog.state`, while typed term/state/mechanism identifiers named in contract definitions or exclusions are also included. This keeps the clean verifier contract self-contained without requiring a claim to repeat the contract's internal vocabulary in its own statement.

This gate is intentionally weaker than natural-language equivalence: a claim may contain additional guarantees or realize several contracts simultaneously. The prohibited case is a claim that references a contract while permitting an execution the contract forbids, such as a path-local proposition claiming to realize an `all_reachable_states` invariant.

## Symbolic assurance boundary

The symbolic package is a formal assurance layer over, but not a replacement for, canonical correctness authority:

> formalize identity, scope, binding, quantification, and relational safety structure while leaving domain meaning in natural-language/domain vocabulary.

The kernel contains no Google Docs-specific concepts. A program declares uninterpreted sorts and typed functions, supplies structured formulas, then asks whether `constraints AND bad_state` is satisfiable.

Interpretation:

- `SAT` — the symbolic specification admits the proposed bad state.
- `UNSAT` — the symbolic specification excludes that bad state within the modeled abstraction.
- Neither result proves that the symbolic abstraction faithfully captures the product design; that remains a semantic-authority question.

The symbolic layer deliberately does not model full transition systems, implementation correspondence, liveness, or arbitrary first-order logic.


### Semantic-contract bridge

`symbolic/bridge.py` connects selected canonical semantic vocabulary and `semantic_contracts` to this IR. Domain-specific mappings live outside the generic kernel, currently in `../symbolic_models/bridge.yaml`. Canonical state may additionally declare `symbolic_dimensions` when a symbolic lowering must preserve coordinates such as document and observation explicitly.

The bridge deliberately does **not** infer formulas from English. Each mapping supplies an explicit structured formula, while deterministic checks prevent silent drift:

- mapped contract class must equal the canonical contract class;
- a `state_invariant` mapping must preserve the canonical `observation_scope`;
- functions used by the formula declare which canonical catalog symbols they represent;
- the union of used catalog symbols must exactly cover the contract's `state_symbols` or `source_symbols + bound_symbols`;
- canonical snake_case vocabulary named directly in a claim statement/formal intent must also remain explicitly anchored in the claim mapping rather than disappearing into anonymous helper predicates;
- if a mapped state symbol declares `symbolic_dimensions`, the symbolic function must expose exactly those ordered dimensions as separate arguments.
- an opaque predicate with no direct `catalog_symbols` must declare canonical `semantic_anchors` and may only be used by those claim/contract subjects; hidden glue cannot be introduced as an unanchored helper merely to obtain UNSAT.

This means the bridge can reject a structurally incomplete or over-scoped translation, but it still cannot prove that an opaque predicate such as `canonical_content_matches` faithfully captures the English definition. That semantic correspondence remains a human/LLM judgment boundary.



### Symbolic composition pilot

`symbolic/composition.py` machine-checks one root/derived claim against its **direct** DAG premises. For target `T` with direct premises `P1..Pn`, the Harness deterministically asks the solver whether:

```text
P1 ∧ ... ∧ Pn ∧ ¬T
```

is satisfiable.

- `UNSAT` => `ENTAILED` within the current symbolic abstraction.
- `SAT` => `COUNTEREXAMPLE`; the solver model witnesses a composition gap or an abstraction/mapping defect.
- Trusted composition requires the target and every direct premise to have current `TRUSTED` translation-assurance records. Missing/unverified premises are never silently omitted.

The adversarial mutation-test path is test-only: it may replace one direct-premise formula with an intentionally weaker formula and verify that a previously `UNSAT` composition becomes `SAT`. This checks that the entailment actually depends on the removed semantic condition rather than succeeding vacuously or because target/premise formulas were accidentally coupled.

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

Claim mappings reuse the typed predicates/vocabulary of already-formalized semantic contracts but carry their own explicit claim formula. Deterministic validation requires a claim mapping to reference exactly the canonical claim's declared `semantic_contracts` and to cover the same catalog-symbol boundary; the same translation-assurance reviewers then determine whether the claim formula is actually equivalent to the claim statement. This allows a narrow leaf claim to project only the part of a broader semantic contract that it actually states.


### Persisted trust and invalidation

`symbolic/assurance_registry.py` persists normalized translation-review results against a SHA-256 signature of the exact semantic source context and symbolic translation under review. The signature includes canonical proposition text, referenced canonical vocabulary/contract context, typed predicate meanings, and the structured formula.

A review record therefore has four meaningful states:

- `TRUSTED` — current source/mapping signature matches and all required direct + round-trip reviewers judged the translation equivalent;
- `REJECTED` / `INCOMPLETE` — semantic disagreement, ambiguity, reviewer failure, or insufficient independent reviews;
- `STALE` — a previously reviewed source or symbolic mapping changed;
- `UNVERIFIED` — no review record exists.

Only `TRUSTED` mappings are eligible for authoritative symbolic proof use. Ordinary `check()` / `exclusion_check()` remain available as untrusted diagnostics; `trusted_check()`, `trusted_exclusion_check()`, and their claim equivalents enforce the registry gate.

### Formal schema and repository preflight

The persisted symbolic layer has closed JSON Schemas separate from the canonical graph schema. They are exposed through:

```text
correctness.py formal-schema bridge
correctness.py formal-schema assurance
correctness.py formal-schema coverage
correctness.py formal-schema composition-artifact
correctness.py formal-schema coverage-artifact
```

`correctness.py validate` treats the formal layer as part of repository health whenever the formal sidecars are present: schema validation runs before semantic bridge checks; trusted coverage and machine-checked composition are rerun; persisted artifacts must match the fresh proof result and current semantic signatures.

### Coverage-facing CLI

The public Harness entry point exposes the symbolic assurance layer without requiring agents to write Python:

- `correctness.py symbolic-status` reports the trust frontier for every mapped contract and claim;
- `correctness.py symbolic-check --kind contract|claim --subject ... --query bad-state.yaml` compares the candidate bad state with and without the selected mapping and, by default, refuses any mapping that is not currently `TRUSTED`;
- `correctness.py symbolic-compose <node>` checks trusted direct-premise entailment for one root/derived claim;
- `correctness.py symbolic-report [node ...]` renders human-readable proof reports for canonical `machine_checked` compositions, including the target statement, direct-premise statements, translation trust, the SMT counterexample query, `SAT`/`UNSAT` meaning, mutation sensitivity, and persisted proof artifacts. With no node arguments it reports every currently machine-checked composition;
- `--allow-untrusted` exists only for debug/research work and must not be treated as authoritative coverage evidence.

This makes the intended coverage integration explicit: LLM agents still propose semantically meaningful bad states, while deterministic SMT checks answer whether trusted symbolic specifications already exclude them.

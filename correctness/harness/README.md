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

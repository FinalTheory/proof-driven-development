# Agent operating contract

This repository treats the correctness model as controlled state, not as ordinary YAML or Markdown.

## Authority chain

For correctness-refinement work, use these sources in order:

1. `correctness/correctness.py workflow-help` — canonical operational protocol.
2. `correctness/graph_schema.py` and the schemas exposed by `correctness.py` — canonical structural contract.
3. `correctness/correctness.yaml` — current system/proof model and campaign state.
4. `design/google-docs.md` — source architecture narrative.
5. `correctness/README.md` — explanatory documentation only.

`correctness/prompt.md` is only a bootstrap prompt for a fresh long-lived orchestrator. It must not duplicate or override the operational contract above.

## Non-negotiable guardrails

- Never edit `correctness/correctness.yaml` directly. All canonical mutations go through `correctness.py mutate`.
- Treat `correctness.yaml` as a closed typed document. Do not invent fields, catalog vocabulary, semantic contracts, verifier kinds, surfaces, or node IDs outside the current schema/tooling.
- Automated proof refinement may reorganize proof structure, but it may not silently invent system semantics. A new protocol mechanism, state variable, lock, phase, generation, transaction boundary, source-of-truth rule, guarantee, or failure assumption requires human semantic judgment unless it is already explicitly approved by the current model.
- A clean verifier must use only `correctness.py audit-prompt <NODE>` as its correctness specification. It must not read the full DAG, source article, repository docs, history, prior audits, sibling results, or old chat context.
- The long-lived orchestrator may read the current repository and judge clean-verifier results, but its own accumulated context is not independent verification evidence.
- Follow the scheduler returned by `refinement-status`; do not infer stop/continue behavior from old prompts or historical files.
- Treat `refinement.nodes.*.status: stable` strictly as refinement maturity for the recorded semantic signature. It is not proof-composition certification and not implementation evidence. Read orthogonal confidence state through `correctness.py assurance-status`; never use assurance metadata itself as a proof premise.

## Local history directory

`history/` is local scratch space, not repository history and not a source of truth. Its contents other than `history/README.md` and `history/.gitignore` are intentionally ignored by Git.

Mutation plans, temporary audit notes, or diagnostics may be placed there when useful. They may be stale and may be deleted at any time. Do not read or replay them by default. Durable project history belongs in Git commits; current correctness semantics belong in `correctness.yaml`.

See `history/README.md` for the local layout convention.

## Validation boundary

For ordinary correctness-DAG/workflow-state mutations, use the validation sequence specified by `workflow-help`.

Run the full tooling regression suite only when correctness tooling itself changes (`correctness.py`, `graph_schema.py`, mutation/validation/scheduler logic, or the test harness), or when validating a tooling repair.

## Repository hygiene

- Keep generated/runtime scratch out of Git unless it is intentionally part of the public artifact.
- Preserve Git history for canonical source files when reorganizing them.
- Do not treat deleted/ignored historical experiments as current architecture.

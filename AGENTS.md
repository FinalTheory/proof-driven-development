# Agent operating contract

This repository treats the correctness model as controlled state, not as ordinary YAML or Markdown.

## Authority chain

For correctness-refinement work, use these sources in order:

1. `correctness/correctness.py workflow-help` — canonical operational protocol.
2. `correctness/graph_schema.py` and the schemas exposed by `correctness.py` — canonical structural contract.
3. `correctness/correctness.yaml` — current system/proof model and campaign state.
4. `design/google-docs.md` — source architecture narrative.
5. `correctness/SPEC.md` — concise human-readable semantic model of the Harness; explanatory, not an operational source of truth.

`correctness/prompts/` contains thin copy/paste launchers for fresh orchestrator windows. They only select a workflow and bootstrap the live Harness; they must not duplicate or override the operational contract above.

## Non-negotiable guardrails

- Never edit `correctness/correctness.yaml` directly. All canonical mutations go through `correctness.py mutate`.
- Treat `correctness.yaml` as a closed typed document. Do not invent fields, catalog vocabulary, semantic contracts, verifier kinds, surfaces, or node IDs outside the current schema/tooling.
- Automated proof refinement may reorganize proof structure, but it may not silently invent system semantics. A new protocol mechanism, state variable, lock, phase, generation, transaction boundary, source-of-truth rule, guarantee, or failure assumption requires human semantic judgment unless it is already explicitly approved by the current model.
- A clean auditor must use only the canonical generated contract for its campaign: `audit-prompt <NODE>` for refinement, `composition-audit-prompt <NODE>` for proof-composition certification, or the role-specific task supplied by a `coverage-audit-prompt` orchestrator. It must obey that contract's isolation boundary and must not import prior audits, sibling results, history, or old chat context.
- The long-lived orchestrator may read the current repository and judge clean-auditor results, but its own accumulated context is not independent verification evidence.
- Follow the scheduler for the active campaign: `refinement-status` for refinement and `composition-status` for proof-composition certification. Global specification coverage is driven by `coverage-audit-prompt`. Do not infer stop/continue behavior from old prompts or historical files.
- Treat `refinement.nodes.*.status: stable` strictly as refinement maturity for the recorded semantic signature. It is not proof-composition certification and not implementation evidence. Read orthogonal confidence state through `correctness.py assurance-status`; never use assurance metadata itself as a proof premise.
- A semantic contract is a reusable property specification / refinement type, not a theorem or proof premise. A claim that references `semantic_contracts` asserts that it itself fully realizes the property's truth condition across at least the contract's minimum semantic scope; do not propagate a contract upward merely because a dependency already realizes it. Such a claim must explicitly pass the generated contract-realization obligation before it can become stable. Compile stable refinement only through the live mutation schema and provide its required `contract_realizations` declaration.

## Temporary workspace

`temp/` is the single repository-local scratch/runtime area. It is fully ignored by Git and is never a source of truth.

Mutation plans, temporary audit notes, or diagnostics may be placed there when useful. They may be stale and may be deleted at any time. Do not read or replay them by default. Durable project history belongs in Git commits; current correctness semantics belong in `correctness.yaml`.

Keep disposable audits, mutation plans, translation-review work, local environments, locks, and generated scratch only under `temp/`.

## Validation boundary

For ordinary correctness-DAG/workflow-state mutations, use the validation sequence specified by `workflow-help`.

Run the full tooling regression suite only when correctness tooling itself changes (`correctness.py`, `graph_schema.py`, mutation/validation/scheduler logic, or the test harness), or when validating a tooling repair.

When a substantial correctness-tooling change alters the Harness concepts or semantic boundaries described in `correctness/SPEC.md`, review that document and update only the affected prose/table rows in the same change. Keep it concise and conceptual; do not add current graph contents or code-level anchors merely to synchronize documentation with implementation. Canonical YAML field/enum drift is checked automatically against `correctness/SCHEMA.md` by the Harness.

## Correctness tooling environment

Correctness commands use the available Python 3 environment; run them with `-B` so bytecode caches are never emitted into the repository.

- Bootstrap or refresh it with `python3 -m pip install -r correctness/requirements.txt`.
- Run correctness commands with `python3 -B correctness/correctness.py ...` from the repository root, or `python3 -B correctness.py ...` from `correctness/`.
- Ensure `correctness/requirements.txt` is installed in the active Python environment before running correctness workflows; repository-local environments, when desired, belong under ignored `temp/`.

## Repository hygiene

- Keep generated/runtime scratch out of Git unless it is intentionally part of the public artifact.
- Preserve Git history for canonical source files when reorganizing them.
- Do not treat deleted/ignored historical experiments as current architecture.

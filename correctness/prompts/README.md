# Audit launchers

These files are thin copy/paste launchers for starting a fresh ChatGPT/Codex window.
They select one correctness workflow and then delegate the actual protocol to the live Harness.
They are not sources of proof semantics and must not duplicate `correctness.py workflow-help` or generated audit contracts.

| Goal | Launcher |
| --- | --- |
| Continue constructing/refining the proof DAG | `refinement.md` |
| Attack the frozen model for missing system-level guarantees | `coverage.md` |
| Certify that every non-leaf's direct premises imply its target | `composition.md` |
| Verify leaf propositions against implementation | Not yet implemented; wait for the executable evidence runner |

Use a fresh window for each campaign. In particular, do not reuse a refinement or composition conversation as a specification-coverage auditor: context separation is part of the assurance model.

The authority order remains:

1. `../correctness.py workflow-help`
2. live schemas / generated Harness contracts
3. `../correctness.yaml`
4. `../../design/google-docs.md`
5. explanatory docs

If a launcher conflicts with the live Harness, the Harness wins.

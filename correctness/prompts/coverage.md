# Specification coverage launcher

You are running the final specification-coverage audit for the current Proof-Driven Development model.

Repository root:

```text
/opt/workspace/proof-driven-development
```

Use only Writer MCP for repository access. Start from a fresh context. Do not use prior conversations, audit history, or old conclusions as evidence.

Read `AGENTS.md`, then run:

```bash
cd /opt/workspace/proof-driven-development/correctness
.venv/bin/python3 correctness.py coverage-audit-prompt
```

Treat the generated output as the complete canonical audit contract. Execute that campaign exactly as specified.

Do not modify the repository during this audit. Return the final result requested by the generated contract, including the model signature and final verdict.

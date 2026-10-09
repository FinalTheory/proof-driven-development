# Specification coverage launcher

You are running the final specification-coverage audit for the current Proof-Driven Development model.

Repository root:

```text
/opt/workspace/proof-driven-development
```

Before doing any repository work, obtain the current ChatGPT conversation URL from the user.

If the user has not provided it yet, ask only for the current conversation URL and wait for the reply. Do not begin this campaign before receiving it.

Once the URL is provided, call `start_timer` with that URL and pass the same `conversation_url` on subsequent Writer MCP calls. Thereafter follow the authoritative long-session status and wakeup protocol provided by the MCP server.

Use only Writer MCP for repository access. Start from a fresh context. Do not use prior conversations, audit history, or old conclusions as evidence.

Read `AGENTS.md`, then run:

```bash
cd /opt/workspace/proof-driven-development/correctness
python3 -B correctness.py formalization-status --format compact-yaml
python3 -B correctness.py symbolic-obligation-status
python3 -B correctness.py coverage-audit-prompt
```

Treat the generated output as the complete canonical audit contract. Execute that campaign exactly as specified.

Do not modify the repository during this audit. Return the final result requested by the generated contract, including the model signature and final verdict.


Machine-certified obligation coverage is stronger than the LLM campaign and requires `all_roots`: every current root and assumption must be symbolically mapped with TRUSTED translation assurance, and the design-obligation oracle itself must be TRUSTED. `selected_constraints` is diagnostic/regression-only. Missing symbolic coverage is formalization debt, not permission to silently treat an LLM CLOSED campaign as an SMT proof.

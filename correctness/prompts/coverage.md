# Specification coverage launcher

You are running the final specification-coverage audit for the current Proof-Driven Development model.

Repository root:

```text
/opt/workspace/proof-driven-development
```

Before doing any repository work, obtain the current ChatGPT conversation URL from the user.

If the user has not provided it yet, ask only for the current conversation URL and wait for the reply. Do not begin this campaign before receiving it.

Once the URL is provided, call `register_conversation_url` exactly once with that URL. After registration, do not pass `conversation_url` to ordinary Writer MCP calls; the MCP server identifies the conversation from OpenAI-provided session metadata. For long-session control tools such as `register_wakeup`, follow the currently exposed tool schema; if it still requires `conversation_url`, use the same already-registered URL rather than treating it as a new registration. The first ordinary Writer MCP call after registration starts the long-session timer, and later calls do not reset it. Thereafter follow the authoritative long-session status and wakeup protocol provided by the MCP server.

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

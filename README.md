# Stealth Startup GitHub triage agent

An issue-triage agent I packaged for Stealth Startup, an AI agent marketplace. It is built with PydanticAI and GitHub's hosted Model Context Protocol (MCP) endpoint (`https://api.githubcopilot.com/mcp/`). It filters the GitHub MCP tools down to a triage set (`list_issues`, `search_code`, `search_issues`, `search_pull_requests`) and returns a structured `IssueProposal` (url, title, summary, should_close, reply_message) for an issue that could be closed in a target repository.

The GitHub MCP server is remote, so nothing is spawned or containerized locally. Python only, no Node.

## What I built

The upstream example is `pydanticai_mcp_github.py` from `Azure-Samples/python-ai-agent-frameworks-demos`. This single-file version (`main.py`) adds:

- a GitHub Models provider path, so the agent runs on one `GITHUB_TOKEN` with no OpenAI spend (the default), next to OpenAI, Azure OpenAI, and Ollama options;
- startup validation of configuration with a clear message per missing variable;
- an error taxonomy mapped to exit codes (0 ok, 1 runtime, 2 config, 3 auth, 4 rate limit, 5 network), so a sandbox orchestrator can tell failure classes apart without parsing tracebacks;
- a pin of `pydantic-ai` to 1.80.0, because the upstream range now resolves to the 2.x line, where the MCP client API was renamed and the import fails.

`STEALTH_STARTUP_UPLOAD_NOTES.txt` documents the environment variables, the token entitlements, the egress allowlist, the caveats, and what was and was not verified.

## How it was verified

In a clean Python 3.11 virtual environment: dependency resolution and every import; each startup-validation and error path (config, auth, rate limit, network), each producing one clean line and the right exit code; and `GITHUB_TOKEN` validity against api.github.com. The full live MCP plus GitHub Models call was not executed in the build environment, because its egress allowlist blocked the two hosts; the notes flag this for first-run verification in the Stealth Startup sandbox.

## Requirements

* Python 3.10 or newer (verified on 3.11)
* A `GITHUB_TOKEN`. It is required in every mode because it authenticates the MCP endpoint, which is gated on **GitHub Copilot** access. In the default `github` mode the same token also serves the LLM, so it must additionally have **GitHub Models** access (`models:read`). A fine-grained PAT with `models:read` on a Copilot-enabled account is the clean setup.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Run

Default path uses GitHub Models for the LLM, so it runs on the GitHub token alone with no OpenAI spend:

```bash
API_HOST=github GITHUB_TOKEN=your_token python main.py
```

Success looks like INFO logs of MCP tool calls followed by a printed `IssueProposal` with all fields populated. Optional: `GITHUB_MODEL` (default `openai/gpt-4o`), `TARGET_REPO` (default `Azure-Samples/azure-search-openai-demo`).

## API_HOST options

* `github` (default): LLM served by GitHub Models, keyed by `GITHUB_TOKEN`. No OpenAI cost.
* `openai`: LLM served by OpenAI.com. Requires `OPENAI_API_KEY` (optional `OPENAI_MODEL`, default `gpt-4o`). The GitHub MCP call still uses `GITHUB_TOKEN`.

## Dependency pin (important)

`pydantic-ai` is pinned to `1.80.0`. The original example declares `pydantic-ai>=1.77.0`, which now resolves to the 2.x line where the MCP client API was renamed and `pydantic_ai.mcp.MCPServerStreamableHTTP` no longer exists, so the agent fails on import. Do not relax this pin.

Exit codes: 0 ok, 1 runtime, 2 config, 3 auth, 4 rate limit, 5 network.

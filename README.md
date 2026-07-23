# PydanticAI GitHub MCP Triage Agent

An issue-triage agent built with **PydanticAI** and GitHub's **hosted MCP endpoint** (`https://api.githubcopilot.com/mcp/`). It filters the GitHub MCP tools down to a triage set (`list_issues`, `search_code`, `search_issues`, `search_pull_requests`) and returns a structured `IssueProposal` (url, title, summary, should_close, reply_message) for an issue that could be closed in a target repository.

The GitHub MCP server is remote, so nothing is spawned or containerized locally. Python only, no Node.

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

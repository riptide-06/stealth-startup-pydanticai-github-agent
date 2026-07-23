"""PydanticAI + GitHub MCP issue-triage agent (Skape packaging).

This agent points a PydanticAI Agent at GitHub's hosted MCP endpoint
(https://api.githubcopilot.com/mcp/), filters the available tools down to a
small triage-focused set, and asks the model to produce a structured
`IssueProposal` describing an issue that could be closed.

LLM provider is selected with the API_HOST environment variable:

    github  (default)  LLM served by GitHub Models (OpenAI-compatible) using
                       the SAME GITHUB_TOKEN that authenticates the MCP call,
                       so the agent runs on a single credential and zero
                       OpenAI spend.
    openai             LLM served by OpenAI.com (needs OPENAI_API_KEY).
    azure              LLM served by Azure OpenAI (needs Azure auth).
    ollama             LLM served by a local Ollama endpoint.

GITHUB_TOKEN is required in every mode because it authenticates the GitHub
MCP endpoint. In `github` mode the same token must additionally carry the
`models:read` scope for GitHub Models.

Original example:
  Azure-Samples/python-ai-agent-frameworks-demos -> examples/pydanticai_mcp_github.py
This file adds a GitHub Models provider path, startup validation, and
production-grade error handling for a sandboxed marketplace deployment.

Usage:
    API_HOST=github GITHUB_TOKEN=... python main.py
"""

import asyncio
import json
import logging
import os
import sys

from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from pydantic_ai import Agent, CallToolsNode, ModelRequestNode
from pydantic_ai.mcp import MCPServerStreamableHTTP
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from rich import print
from rich.logging import RichHandler

logging.basicConfig(level=logging.WARNING, format="%(message)s", datefmt="[%X]", handlers=[RichHandler()])
logger = logging.getLogger("pydanticai_mcp_github")

GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
GITHUB_MODELS_URL = "https://models.github.ai/inference"
VALID_HOSTS = ("github", "openai", "azure", "ollama")

# Exit codes so a sandbox orchestrator can distinguish failure classes.
EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_CONFIG = 2
EXIT_AUTH = 3
EXIT_RATE_LIMIT = 4
EXIT_NETWORK = 5


class ConfigError(Exception):
    """Raised for missing/invalid configuration discovered at startup."""


class IssueProposal(BaseModel):
    """Structured proposal for closing an issue."""

    url: str = Field(description="URL of the issue")
    title: str = Field(description="Title of the issue")
    summary: str = Field(description="Brief summary of the issue and signals for closing")
    should_close: bool = Field(description="Whether the issue should be closed or not")
    reply_message: str = Field(description="Message to post when closing the issue, if applicable")


# --------------------------------------------------------------------------- #
# Startup validation
# --------------------------------------------------------------------------- #
def validate_config(api_host: str) -> None:
    """Fail fast with a clear message if required config is missing.

    Raises ConfigError (never dumps a traceback) so the caller can print a
    clean line and exit with EXIT_CONFIG.
    """
    if api_host not in VALID_HOSTS:
        raise ConfigError(
            f"API_HOST='{api_host}' is not valid. Set one of: {', '.join(VALID_HOSTS)} "
            f"(default: github)."
        )

    # GITHUB_TOKEN authenticates the MCP endpoint in every mode.
    if not os.getenv("GITHUB_TOKEN"):
        raise ConfigError(
            "GITHUB_TOKEN is not set. It is required to authenticate the GitHub "
            "MCP endpoint (https://api.githubcopilot.com/mcp/). In 'github' mode "
            "the same token must also have the 'models:read' scope for GitHub Models."
        )

    missing: list[str] = []
    if api_host == "openai":
        if not os.getenv("OPENAI_API_KEY"):
            missing.append("OPENAI_API_KEY")
    elif api_host == "azure":
        if not os.getenv("AZURE_OPENAI_ENDPOINT"):
            missing.append("AZURE_OPENAI_ENDPOINT")
        if not os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT"):
            missing.append("AZURE_OPENAI_CHAT_DEPLOYMENT")
    elif api_host == "ollama":
        if not os.getenv("OLLAMA_MODEL"):
            missing.append("OLLAMA_MODEL")

    if missing:
        raise ConfigError(
            f"API_HOST='{api_host}' requires: {', '.join(missing)}. "
            f"Set the variable(s) or switch API_HOST to 'github' to run on the GitHub token alone."
        )


def build_model(api_host: str):
    """Construct the pydantic-ai model for the selected provider.

    Returns (model, async_credential). async_credential is non-None only for
    Azure and must be closed by the caller.
    """
    async_credential = None

    if api_host == "github":
        # Primary path: GitHub Models (OpenAI-compatible) on the GITHUB_TOKEN.
        model_name = os.getenv("GITHUB_MODEL", "openai/gpt-4o")
        client = AsyncOpenAI(base_url=GITHUB_MODELS_URL, api_key=os.environ["GITHUB_TOKEN"])
        model = OpenAIChatModel(model_name, provider=OpenAIProvider(openai_client=client))

    elif api_host == "openai":
        # Fallback path: real OpenAI.com key.
        model_name = os.getenv("OPENAI_MODEL", "gpt-4o")
        client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
        model = OpenAIChatModel(model_name, provider=OpenAIProvider(openai_client=client))

    elif api_host == "azure":
        # Lazy import: azure-identity is only needed on this path, so the
        # package still runs in a sandbox that never touches Azure.
        from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

        async_credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            async_credential, "https://cognitiveservices.azure.com/.default"
        )
        client = AsyncOpenAI(
            base_url=os.environ["AZURE_OPENAI_ENDPOINT"] + "/openai/v1",
            api_key=token_provider,
        )
        model = OpenAIChatModel(
            os.environ["AZURE_OPENAI_CHAT_DEPLOYMENT"], provider=OpenAIProvider(openai_client=client)
        )

    else:  # ollama
        client = AsyncOpenAI(
            base_url=os.environ.get("OLLAMA_ENDPOINT", "http://localhost:11434/v1"), api_key="none"
        )
        model = OpenAIChatModel(os.environ["OLLAMA_MODEL"], provider=OpenAIProvider(openai_client=client))

    return model, async_credential


# --------------------------------------------------------------------------- #
# Error classification (clean messages, no raw tracebacks)
# --------------------------------------------------------------------------- #
def _flatten(exc: BaseException):
    """Yield exc and, for ExceptionGroups (raised by MCP/anyio task groups),
    every nested leaf exception."""
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            yield from _flatten(sub)
    else:
        yield exc


def _status_code(exc: BaseException):
    """Best-effort extraction of an HTTP status code from an exception."""
    code = getattr(exc, "status_code", None)
    if code is not None:
        return code
    resp = getattr(exc, "response", None)
    if resp is not None:
        return getattr(resp, "status_code", None)
    return None


def classify_error(exc: BaseException) -> tuple[int, str]:
    """Map an exception (or ExceptionGroup) to (exit_code, friendly_message)."""
    saw_network = False
    for e in _flatten(exc):
        name = type(e).__name__
        code = _status_code(e)

        if code in (401, 403) or name in ("AuthenticationError", "PermissionDeniedError"):
            return (
                EXIT_AUTH,
                "Authentication failed (HTTP 401/403). Check that GITHUB_TOKEN is valid and "
                "has BOTH GitHub Copilot MCP access (for api.githubcopilot.com) AND the "
                "'models:read' scope (for GitHub Models). If you supplied OPENAI_API_KEY, verify it.",
            )
        if code == 429 or name == "RateLimitError":
            return (
                EXIT_RATE_LIMIT,
                "Rate limited (HTTP 429). GitHub Models' free tier is roughly 15 requests/min and "
                "150/day (lower for some models). Wait and retry, slow the request rate, or switch "
                "API_HOST=openai with a paid OPENAI_API_KEY.",
            )
        if name in ("APIConnectionError", "APITimeoutError", "ConnectError", "ConnectTimeout",
                    "ReadTimeout", "ReadError", "ProxyError"):
            saw_network = True

    if saw_network:
        return (
            EXIT_NETWORK,
            "Network/egress failure reaching the LLM or MCP endpoint. The sandbox must allow "
            "outbound HTTPS to api.githubcopilot.com and models.github.ai (or your chosen LLM host).",
        )

    # Unknown failure: one concise line, no stack dump.
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return (EXIT_RUNTIME, f"Unexpected error: {msg}")


# --------------------------------------------------------------------------- #
# Agent run
# --------------------------------------------------------------------------- #
async def run_agent(api_host: str) -> int:
    model, async_credential = build_model(api_host)

    server = MCPServerStreamableHTTP(
        url=GITHUB_MCP_URL,
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"},
    )
    desired_tool_names = ("list_issues", "search_code", "search_issues", "search_pull_requests")
    filtered_tools = server.filtered(lambda ctx, tool_def: tool_def.name in desired_tool_names)

    agent: Agent[None, IssueProposal] = Agent(
        model,
        system_prompt=(
            "You are an issue triage assistant. Use the provided tools to find an issue that can be closed "
            "and produce an IssueProposal."
        ),
        output_type=IssueProposal,
        toolsets=[filtered_tools],
    )

    target_repo = os.getenv("TARGET_REPO", "Azure-Samples/azure-search-openai-demo")
    user_content = f"Find an issue from {target_repo} that can be closed."

    try:
        async with agent.iter(user_content) as agent_run:
            async for node in agent_run:
                if isinstance(node, CallToolsNode):
                    tool_call = node.model_response.parts[0]
                    logger.info(f"Calling tool '{getattr(tool_call, 'tool_name', '?')}' with args:\n"
                                f"{getattr(tool_call, 'args', '')}")
                elif isinstance(node, ModelRequestNode) and isinstance(node.request.parts[0], ToolReturnPart):
                    tool_return_value = json.dumps(node.request.parts[0].content)
                    logger.info(f"Got tool result:\n{tool_return_value[0:200]}...")

        print(agent_run.result.output)
        return EXIT_OK
    finally:
        if async_credential is not None:
            await async_credential.close()


def main() -> int:
    load_dotenv(override=True)
    logger.setLevel(logging.INFO)
    api_host = os.getenv("API_HOST", "github").strip().lower()

    try:
        validate_config(api_host)
    except ConfigError as e:
        logger.error(f"[config] {e}")
        return EXIT_CONFIG

    logger.info(f"Starting issue-triage agent (API_HOST={api_host}, "
                f"MCP={GITHUB_MCP_URL})")

    try:
        return asyncio.run(run_agent(api_host))
    except KeyboardInterrupt:
        logger.error("Interrupted.")
        return EXIT_RUNTIME
    except BaseException as exc:  # noqa: BLE001 - deliberate top-level guard
        exit_code, message = classify_error(exc)
        logger.error(message)
        return exit_code


if __name__ == "__main__":
    sys.exit(main())

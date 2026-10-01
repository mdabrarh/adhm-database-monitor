"""
Thin wrapper around the Claude Agent SDK. Isolated in its own file
deliberately: the SDK's public API has moved before and will likely move
again, and this is the one place you should need to touch when it does.

If you hit an error here after `pip install -U claude-agent-sdk`, run:
    python -c "from claude_agent_sdk import ClaudeAgentOptions; help(ClaudeAgentOptions)"
and adjust the field names in _build_options() to match what your installed
version actually exposes -- the concepts (system prompt, mcp_servers,
allowed_tools, max_turns, a non-interactive permission mode) are stable
even when exact field names drift between releases.

Docs: https://code.claude.com/docs/en/agent-sdk/python
"""
from __future__ import annotations

import json
import logging
import re
import sys

from claude_agent_sdk import ClaudeAgentOptions, query

from agent.prompts import SYSTEM_PROMPT

logger = logging.getLogger("db_monitor.claude_client")


def _mcp_server_spec(server_name: str, script_path: str, env: dict) -> dict:
    return {
        "type": "stdio",
        "command": sys.executable,  # use the same venv's python that's running this process
        "args": [script_path],
        "env": env,
    }


def _build_options(
    instance_env: dict,
    baseline_db_path: str,
    engine_script_path: str,
    baseline_script_path: str,
    allowed_tool_names: list[str],
    model: str,
    max_turns: int,
) -> "ClaudeAgentOptions":
    baseline_env = {"BASELINE_DB_PATH": baseline_db_path, "DB_INSTANCE_LABEL": instance_env["DB_INSTANCE_LABEL"]}

    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        model=model,
        mcp_servers={
            "dbserver": _mcp_server_spec("dbserver", engine_script_path, instance_env),
            "baseline": _mcp_server_spec("baseline", baseline_script_path, baseline_env),
        },
        allowed_tools=allowed_tool_names,
        max_turns=max_turns,
        # Non-interactive: this runs unattended on a server, so tool calls
        # (all read-only, per the mcp_servers above) must not block waiting
        # on a human to approve them. If your installed SDK version uses a
        # different value for "don't ask, just allow the configured tools",
        # this is the string to change.
        permission_mode="bypassPermissions",
    )


def _extract_final_text(messages: list) -> str:
    """Prefer ResultMessage.result (the SDK's own "here's the final answer"
    field) since it's the most direct signal; fall back to the last
    TextBlock across AssistantMessage.content if a ResultMessage wasn't
    emitted for some reason (older/newer SDK versions, or an error path)."""
    for message in messages:
        result_text = getattr(message, "result", None)
        if result_text:
            return result_text

    final_text = ""
    for message in messages:
        content = getattr(message, "content", None)
        if not content:
            continue
        try:
            for block in content:
                text = getattr(block, "text", None)
                if text:
                    final_text = text
        except TypeError:
            final_text = str(content)
    return final_text


def _extract_json(text: str) -> dict:
    """The system prompt asks for JSON-only output, but models sometimes
    wrap it in markdown fences anyway -- strip those defensively, then grab
    the first {...} block."""
    text = text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)
    else:
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            text = brace_match.group(0)
    return json.loads(text)


async def run_agent_query(
    *,
    instance_env: dict,
    baseline_db_path: str,
    engine: str,
    user_prompt: str,
    mcp_servers_dir: str,
    model: str,
    max_turns: int,
) -> dict:
    """Runs one non-interactive agent turn against the given instance and
    returns the parsed verdict dict: {tier, confidence, summary,
    signals_used, recommended_action}.

    Raises json.JSONDecodeError if the model's final response couldn't be
    parsed as the expected JSON -- callers should catch this, log the raw
    text, and treat it as a "no verdict this cycle" rather than crash the
    whole monitoring loop over one bad response.
    """
    engine_script = f"{mcp_servers_dir}/{'postgres_server.py' if engine == 'postgres' else 'mysql_server.py'}"
    baseline_script = f"{mcp_servers_dir}/baseline_server.py"

    engine_tools = [
        "mcp__dbserver__get_blocking_chains",
        "mcp__dbserver__get_undo_pressure",
        "mcp__dbserver__get_wait_events",
        "mcp__dbserver__get_long_transactions",
    ]
    engine_tools.append(
        "mcp__dbserver__get_deadlock_count" if engine == "postgres" else "mcp__dbserver__get_latest_deadlock"
    )
    allowed_tools = engine_tools + [
        "mcp__baseline__get_historical_baseline",
        "mcp__baseline__get_recent_decisions",
    ]

    options = _build_options(
        instance_env=instance_env,
        baseline_db_path=baseline_db_path,
        engine_script_path=engine_script,
        baseline_script_path=baseline_script,
        allowed_tool_names=allowed_tools,
        model=model,
        max_turns=max_turns,
    )

    messages = []
    async for message in query(prompt=user_prompt, options=options):
        messages.append(message)

    final_text = _extract_final_text(messages)
    logger.debug("Raw agent response: %s", final_text)
    return _extract_json(final_text)

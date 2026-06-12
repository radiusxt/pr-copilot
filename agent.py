"""Core Agent class — orchestrates the PR merge monitor loop."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clients import AnthropicClient, MonitorConfig, ROOT_DIR, ensure_logs_dir
from tools import Tool, ToolResult, build_tool_registry
 
# Markdown filepath containing the system prompt fed to the LLM on every completion request.
# If the file is absent, the agent falls back to a hardcoded one-liner so the loop can still run without the prompts directory.
SYSTEM_PROMPT_PATH = ROOT_DIR / "prompts" / "monitor.md"


@dataclass
class AgentState:
    """
    Mutable snapshot of everything the agent needs to carry between iterations.
 
    Attributes:
        iteration:      Running count of how many times run_once() has been called.
        last_status:    The raw PR-status payload returned by the most recent get_pr_status tool call, or None before the first observation.
        last_comments:  The raw comments payload returned by the most recent get_pr_comments tool call, or None before the first observation.
        history:        Ordered list of IterationResult dicts recorded by _record(), mirroring what is also written to logs/agent-history.jsonl.
    """
    iteration: int = 0
    last_status: dict[str, Any] | None = None
    last_comments: dict[str, Any] | None = None
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class IterationResult:
    """
    Structured summary produced at the end of each run_once() call.
 
    Attributes:
        status:          High-level outcome label. Either "merge-ready", "in-progress", or "blocked".
        merge_ready:     True when the PR is ready to be merged after this iteration.
        blocked_reasons: List of blocking reason strings taken directly from the
                         PR-status payload (e.g. "ci_failing", "review_required").
        actions:         Human-readable log of every tool call or LLM action taken
                         during this iteration.
        human_needed:    True when the agent has determined it cannot unblock the PR
                         on its own and a human must intervene.
        message:         Short prose summary suitable for displaying in the terminal UI.
    """
    status: str
    merge_ready: bool
    blocked_reasons: list[str]
    actions: list[str]
    human_needed: bool
    message: str


def _interval_to_seconds(raw: str) -> int:
    """
    Converts a human-readable interval string into a number of seconds.
 
    Recognised unit suffixes:
        s: seconds
        m: minutes
        h: hours
        d: days
    
    All other strings fall back to 300 seconds (5 minutes).
 
    Args:
        raw: The interval string read from the loop config (e.g. "10m").
 
    Returns:
        The equivalent duration in whole seconds.
    """
    raw = (raw or "5m").strip()

    if len(raw) < 2:
        return 300

    unit = raw[-1]

    try:
        value = int(raw[:-1])
    except ValueError:
        return 300

    return {"s": value, "m": value * 60, "h": value * 3600, "d": value * 86400}.get(unit, 300)

def _status_fingerprint(status: dict[str, Any] | None) -> str:
    """
    Produces a compact string uniquely representing the current PR state.
 
    The fingerprint is used in dynamic-mode polling to detect whether anything meaningful has changed since last observation. 
    If the fingerprint is identical the agent skips a full iteration and sleeps instead.
 
    Encoded fields:
        merge_state: overall GitHub merge-state string
        mergeable: the raw mergeable flag from the API
        #failing CI: count of failing CI checks
        #pending CI: count of pending CI checks
        review_decision: the repository's review-decision string
 
    Args:
        status: PR-status payload from get_pr_status, or None.
 
    Returns:
        A pipe-delimited string of the five field values, or "unknown" if no status has been observed yet.
    """
    if not status:
        return "unknown"

    ci = status.get("ci", {})

    return "|".join(
        [
            str(status.get("merge_state")),
            str(status.get("mergeable")),
            str(len(ci.get("failing", []))),
            str(len(ci.get("pending", []))),
            str(status.get("review_decision")),
        ]
    )


class Agent:
    """Manages state, LLM interaction, and tool execution for PR merge monitoring."""

    def __init__(
        self,
        config: MonitorConfig,
        *,
        cwd: Path | None = None,
        use_llm: bool = True,
        use_docker: bool = False,
        max_tool_rounds: int = 8,
    ) -> None:
        """
        Initialises the agent and wires up all dependencies.
 
        Args:
            config: Parsed monitor configuration (repo, PR number, guardrails, loop settings, etc.).
            cwd: Working directory used by tools that shell out or write files. Defaults to the current process directory.
            use_llm: When False the agent skips LLM calls entirely and only records observations which is useful for dry-run / testing.
            use_docker: When True, tools that execute commands will run them inside a Docker container rather than on the host.
            max_tool_rounds: Maximum number of LLM to tool call cycles permitted per iteration before the loop exits regardless of LLM intent.
                    This is to guard against infinite tool-call loops.
        """
        self.config = config
        self.cwd = cwd or Path.cwd()
        self.use_llm = use_llm
        self.max_tool_rounds = max_tool_rounds
        self.state = AgentState()
        self.tools = build_tool_registry(config, cwd=self.cwd, use_docker=use_docker)
        self.llm: AnthropicClient | None = None

        if use_llm:
            try:
                self.llm = AnthropicClient()
            except RuntimeError:
                self.llm = None

    @property
    def system_prompt(self) -> str:
        """
        Return the system prompt text accompanying every LLM request.
        Reads from SYSTEM_PROMPT_PATH on each access so that edits to the prompt file take effect without restarting the agent.
        Falls back to a minimal hardcoded string when the file does not exist.
        """
        if SYSTEM_PROMPT_PATH.exists():
            return SYSTEM_PROMPT_PATH.read_text()

        return "Keep the configured GitHub PR merge-ready."

    def observe(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """
        Run the observation tools and update agent state with the latest PR data.
        Calls get_pr_status() followed by get_pr_comments(), stores the results in self.state, and returns them for immediate use by the caller.
 
        Returns:
            A (status, comments) tuple of the raw tool-response payloads.
 
        Raises:
            RuntimeError: If either tool fails to return structured data, which makes any subsequent LLM reasoning unreliable.
        """
        status_result = self.tools["get_pr_status"].run()
        comments_result = self.tools["get_pr_comments"].run()

        if not status_result.data or not comments_result.data:
            raise RuntimeError("Observation tools failed to return structured data.")
            
        self.state.last_status = status_result.data
        self.state.last_comments = comments_result.data
        return status_result.data, comments_result.data

    def _dispatch_tool(self, name: str, tool_input: dict[str, Any]) -> ToolResult:
        """
        Look up a tool by name and execute it with the provided input.
        Used by the inner LLM loop to route tool-use blocks to the correct Tool implementation.
 
        Args:
            name: The tool name as declared in the tool registry and in the tool definition sent to the LLM.
            tool_input: Keyword arguments dict forwarded directly to tool.run().
 
        Returns:
            A ToolResult indicating success/failure and carrying the tool's output.
            Returns a failed ToolResult (rather than raising) for unknown names so the LLM can see the error and decide how to proceed.
        """
        tool = self.tools.get(name)

        if not tool:
            return ToolResult(ok=False, output=f"Unknown tool: {name}")

        return tool.run(**tool_input)

    def _run_llm_loop(self, status: dict[str, Any], comments: dict[str, Any]) -> list[str]:
        """
        Drive the multi-turn LLM to tool-execution cycle for a single iteration.
 
        Builds an initial user message from the current PR context, then repeatedly sends messages to the LLM and processes any tool-use blocks
        it returns until either the model stops requesting tools or max_tool_rounds is reached.
 
        Each tool call is dispatched via _dispatch_tool and its result is fed back to the model as a tool_result content block.
        This allows the LLM to chain multiple actions (e.g. inspect CI logs → post comment → re-trigger workflow) within one iteration.
 
        Args:
            status: Current PR-status payload from observe().
            comments: Current PR-comments payload from observe().
 
        Returns:
            A list of human-readable action strings describing every tool call made and the LLM's final textual conclusion, suitable for inclusion in an IterationResult.
        """
        if not self.llm:
            return ["LLM unavailable — observation only."]

        actions: list[str] = []
        user_content = (
            "Current PR context:\n\n"
            f"```json\n{json.dumps(status, indent=2)}\n```\n\n"
            f"```json\n{json.dumps(comments, indent=2)}\n```\n\n"
            "Use tools to fix blocking issues within guardrails. Start by diagnosing, then act."
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
        tool_defs = [tool.as_anthropic_tool() for tool in self.tools.values()]

        for _ in range(self.max_tool_rounds):
            response = self.llm.complete_with_tools(
                system=self.system_prompt,
                messages=messages,
                tools=tool_defs,
            )

            # Separate text blocks (narrative / final answer) from tool-use blocks and rebuild the assistant turn
            # in the canonical format expected by the Anthropic multi-turn API.
            assistant_content: list[dict[str, Any]] = []
            tool_uses: list[Any] = []

            for block in response.content:
                if block.type == "text" and block.text.strip():
                    assistant_content.append({"type": "text", "text": block.text})

                elif block.type == "tool_use":
                    tool_uses.append(block)
                    assistant_content.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }
                    )

            messages.append({"role": "assistant", "content": assistant_content})

            if not tool_uses:
                if assistant_content:
                    actions.append(assistant_content[0].get("text", "LLM completed without tools."))

                break

            tool_results: list[dict[str, Any]] = []

            for tool_use in tool_uses:
                result = self._dispatch_tool(tool_use.name, tool_use.input or {})
                actions.append(f"{tool_use.name}: {'ok' if result.ok else 'failed'}")
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": result.output[:12000],
                        "is_error": not result.ok,
                    }
                )
            
            messages.append({"role": "user", "content": tool_results})

        return actions

    def run_once(self) -> IterationResult:
        """
        Execute a single agent iteration: observe → act → re-observe → evaluate.
 
        Workflow:
            1. Call observe() to get the latest PR state.
            2. If the PR is already merge-ready, return immediately without
               invoking the LLM (avoids unnecessary API calls).
            3. Otherwise run the LLM loop (_run_llm_loop) to attempt to unblock
               the PR, then observe again to see whether it worked.
            4. Determine whether human intervention is required based on the
               blocking reasons that remain after the LLM's actions.
            5. Record the result to history/logs and return it.
 
        Returns:
            An IterationResult summarising what happened and the new PR state.

        References:
            https://dev.to/adgapar/a-loop-is-all-you-need-building-conversation-ai-agents-1039
        """
        self.state.iteration += 1
        status, comments = self.observe()

        if status.get("merge_ready"):
            result = IterationResult(
                status="merge-ready",
                merge_ready=True,
                blocked_reasons=[],
                actions=["Observed PR — already merge-ready."],
                human_needed=False,
                message="PR is merge-ready.",
            )
            self._record(result)
            return result

        actions = self._run_llm_loop(status, comments)
        status, _comments = self.observe()

        blocked = status.get("blocked_reasons", [])
        human_needed = any(
            reason in blocked
            for reason in ("merge_conflicts", "changes_requested", "review_required")
        ) or (
            "ci_failing" in blocked
            and not self.config.guardrails.get("allow_unrelated_changes")
        )

        if status.get("merge_ready"):
            iteration_status = "merge-ready"
            message = "PR became merge-ready after this iteration."
        elif human_needed and not actions:
            iteration_status = "blocked"
            message = "Human input required before continuing."
        else:
            iteration_status = "in-progress"
            message = "Iteration complete; PR still has open items."

        result = IterationResult(
            status=iteration_status,
            merge_ready=bool(status.get("merge_ready")),
            blocked_reasons=blocked,
            actions=actions,
            human_needed=human_needed and not status.get("merge_ready"),
            message=message,
        )
        self._record(result)
        return result

    def run_loop(self, *, mode: str | None = None, max_iterations: int | None = None) -> None:
        loop_cfg = self.config.loop
        mode = mode or loop_cfg.get("mode", "fixed")
        poll_seconds = int(loop_cfg.get("ci_poll_seconds", 30))
        interval_seconds = _interval_to_seconds(loop_cfg.get("interval", "5m"))

        iterations = 0
        last_fp = _status_fingerprint(self.state.last_status)

        while True:
            result = self.run_once()
            self._print_result(result)
            iterations += 1

            if result.merge_ready:
                break
            if max_iterations and iterations >= max_iterations:
                break
            if result.human_needed and result.status == "blocked":
                break

            if mode == "dynamic":
                status, _ = self.observe()
                fp = _status_fingerprint(status)
                if fp == last_fp:
                    time.sleep(poll_seconds)
                    continue
                last_fp = fp
            else:
                time.sleep(interval_seconds)

    def _record(self, result: IterationResult) -> None:
        entry = {
            "iteration": self.state.iteration,
            "status": result.status,
            "merge_ready": result.merge_ready,
            "blocked_reasons": result.blocked_reasons,
            "actions": result.actions,
            "human_needed": result.human_needed,
            "message": result.message,
        }
        self.state.history.append(entry)
        ensure_logs_dir()
        log_path = ROOT_DIR / "logs" / "agent-history.jsonl"
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def _print_result(self, result: IterationResult) -> None:
        print()
        print(f"Status: {result.status}")
        print(f"Merge ready: {result.merge_ready}")
        print(f"Blocked: {', '.join(result.blocked_reasons) or 'none'}")
        print(f"Human needed: {'yes' if result.human_needed else 'no'}")
        print(f"Message: {result.message}")
        if result.actions:
            print("Actions:")
            for action in result.actions:
                print(f"  - {action}")

"""Core Agent class — orchestrates the PR babysit loop."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clients import AnthropicClient, BabysitConfig, ROOT_DIR, ensure_logs_dir
from tools import Tool, ToolResult, build_tool_registry

SYSTEM_PROMPT_PATH = ROOT_DIR / "prompts" / "babysit.md"


@dataclass
class AgentState:
    iteration: int = 0
    last_status: dict[str, Any] | None = None
    last_comments: dict[str, Any] | None = None
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class IterationResult:
    status: str
    merge_ready: bool
    blocked_reasons: list[str]
    actions: list[str]
    human_needed: bool
    message: str


def _interval_to_seconds(raw: str) -> int:
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
    """Manages state, LLM interaction, and tool execution for PR babysitting."""

    def __init__(
        self,
        config: BabysitConfig,
        *,
        cwd: Path | None = None,
        use_llm: bool = True,
        use_docker: bool = False,
        max_tool_rounds: int = 8,
    ) -> None:
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
        if SYSTEM_PROMPT_PATH.exists():
            return SYSTEM_PROMPT_PATH.read_text()
        return "Keep the configured GitHub PR merge-ready."

    def observe(self) -> tuple[dict[str, Any], dict[str, Any]]:
        status_result = self.tools["get_pr_status"].run()
        comments_result = self.tools["get_pr_comments"].run()
        if not status_result.data or not comments_result.data:
            raise RuntimeError("Observation tools failed to return structured data.")
        self.state.last_status = status_result.data
        self.state.last_comments = comments_result.data
        return status_result.data, comments_result.data

    def _dispatch_tool(self, name: str, tool_input: dict[str, Any]) -> ToolResult:
        tool = self.tools.get(name)
        if not tool:
            return ToolResult(ok=False, output=f"Unknown tool: {name}")
        return tool.run(**tool_input)

    def _run_llm_loop(self, status: dict[str, Any], comments: dict[str, Any]) -> list[str]:
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

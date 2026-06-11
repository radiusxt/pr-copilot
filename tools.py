"""Tools the PR babysit agent can invoke."""

from __future__ import annotations

import json
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from clients import BabysitConfig, DockerClient, GitHubClient, ensure_logs_dir


@dataclass
class ToolResult:
    ok: bool
    output: str
    data: dict[str, Any] | None = None


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]

    @abstractmethod
    def schema(self) -> dict[str, Any]:
        """Anthropic-compatible tool input schema."""

    @abstractmethod
    def run(self, **kwargs: Any) -> ToolResult:
        """Execute the tool."""

    def as_anthropic_tool(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.schema(),
        }


def _blocked_workflow_change(path: str, config: BabysitConfig) -> bool:
    if config.guardrails.get("allow_ci_workflow_changes"):
        return False
    normalized = path.replace("\\", "/")
    return normalized.startswith(".github/workflows/")


class PRStatusTool(Tool):
    name = "get_pr_status"
    description = "Fetch CI, merge state, and merge-readiness for the configured PR."

    def __init__(self, github: GitHubClient, config: BabysitConfig) -> None:
        self.github = github
        self.config = config

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    def run(self, **_kwargs: Any) -> ToolResult:
        data = self.github.fetch_pr()
        checks = data.get("statusCheckRollup") or []
        failing = [
            c
            for c in checks
            if (c.get("conclusion") or "").lower()
            in ("failure", "cancelled", "timed_out", "action_required")
        ]
        pending = [
            c
            for c in checks
            if (c.get("status") or "").lower() in ("queued", "in_progress", "pending")
        ]
        merge_state = data.get("mergeStateStatus")
        review_decision = data.get("reviewDecision")
        summary = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "repo": self.config.repo,
            "pr": self.config.pr,
            "title": data.get("title"),
            "url": data.get("url"),
            "state": data.get("state"),
            "base": data.get("baseRefName"),
            "head": data.get("headRefName"),
            "mergeable": data.get("mergeable"),
            "merge_state": merge_state,
            "review_decision": review_decision,
            "ci": {
                "total_checks": len(checks),
                "failing": [
                    {
                        "name": c.get("name"),
                        "conclusion": c.get("conclusion"),
                        "detailsUrl": c.get("detailsUrl"),
                    }
                    for c in failing
                ],
                "pending": [{"name": c.get("name"), "status": c.get("status")} for c in pending],
                "all_green": len(checks) > 0 and not failing and not pending,
            },
            "merge_ready": (
                data.get("state") == "OPEN"
                and data.get("mergeable") == "MERGEABLE"
                and merge_state == "CLEAN"
                and review_decision in ("APPROVED", None)
                and (len(checks) == 0 or (not failing and not pending))
            ),
            "blocked_reasons": [],
        }
        if merge_state == "BEHIND":
            summary["blocked_reasons"].append("branch_behind_base")
        if merge_state == "DIRTY":
            summary["blocked_reasons"].append("merge_conflicts")
        if failing:
            summary["blocked_reasons"].append("ci_failing")
        if pending:
            summary["blocked_reasons"].append("ci_pending")
        if review_decision == "CHANGES_REQUESTED":
            summary["blocked_reasons"].append("changes_requested")
        if review_decision == "REVIEW_REQUIRED":
            summary["blocked_reasons"].append("review_required")

        logs = ensure_logs_dir()
        out = logs / "latest-status.json"
        out.write_text(json.dumps(summary, indent=2))
        return ToolResult(ok=True, output=json.dumps(summary, indent=2), data=summary)


class PRCommentsTool(Tool):
    name = "get_pr_comments"
    description = "Fetch unresolved review threads for the configured PR."

    def __init__(self, github: GitHubClient) -> None:
        self.github = github

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    def run(self, **_kwargs: Any) -> ToolResult:
        unresolved = self.github.fetch_unresolved_threads()
        payload = {
            "unresolved_thread_count": len(unresolved),
            "unresolved_threads": unresolved,
        }
        logs = ensure_logs_dir()
        out = logs / "latest-comments.json"
        out.write_text(json.dumps(payload, indent=2))
        return ToolResult(ok=True, output=json.dumps(payload, indent=2), data=payload)


class ShellCommandTool(Tool):
    name = "run_shell_command"
    description = (
        "Run a shell command in the target repo (tests, lint, git operations). "
        "Blocked for .github/workflows unless explicitly allowed in config."
    )

    def __init__(self, config: BabysitConfig, cwd: Path | None = None) -> None:
        self.config = config
        self.cwd = cwd or Path.cwd()

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
            },
            "required": ["command"],
        }

    def run(self, *, command: str, **_kwargs: Any) -> ToolResult:
        if "git push" in command and not self.config.guardrails.get("auto_push"):
            return ToolResult(
                ok=False,
                output="git push blocked by guardrails (auto_push: false). Ask the human to approve.",
            )
        if ".github/workflows" in command and not self.config.guardrails.get(
            "allow_ci_workflow_changes"
        ):
            return ToolResult(ok=False, output="Workflow changes are blocked by guardrails.")

        result = subprocess.run(
            command,
            shell=True,
            cwd=self.cwd,
            capture_output=True,
            text=True,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return ToolResult(ok=result.returncode == 0, output=output or "(no output)")


class WriteFileTool(Tool):
    name = "write_file"
    description = "Create or update a file in the target repo with the given content."

    def __init__(self, config: BabysitConfig, cwd: Path | None = None) -> None:
        self.config = config
        self.cwd = cwd or Path.cwd()

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        }

    def run(self, *, path: str, content: str, **_kwargs: Any) -> ToolResult:
        if _blocked_workflow_change(path, self.config):
            return ToolResult(ok=False, output=f"Blocked workflow edit: {path}")
        target = (self.cwd / path).resolve()
        if not str(target).startswith(str(self.cwd.resolve())):
            return ToolResult(ok=False, output="Path escapes working directory.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return ToolResult(ok=True, output=f"Wrote {path} ({len(content)} bytes)")


class DockerExecTool(Tool):
    name = "docker_exec"
    description = "Run a command inside a Docker container with the repo mounted at /workspace."

    def __init__(self, docker: DockerClient) -> None:
        self.docker = docker

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command to run in the container"},
            },
            "required": ["command"],
        }

    def run(self, *, command: str, **_kwargs: Any) -> ToolResult:
        try:
            output = self.docker.run(command)
            return ToolResult(ok=True, output=output)
        except RuntimeError as exc:
            return ToolResult(ok=False, output=str(exc))


def build_tool_registry(
    config: BabysitConfig,
    *,
    cwd: Path | None = None,
    use_docker: bool = False,
    docker_image: str = "node:20-bookworm",
) -> dict[str, Tool]:
    github = GitHubClient(config)
    tools: list[Tool] = [
        PRStatusTool(github, config),
        PRCommentsTool(github),
        ShellCommandTool(config, cwd=cwd),
        WriteFileTool(config, cwd=cwd),
    ]
    if use_docker:
        tools.append(DockerExecTool(DockerClient(docker_image)))
    return {tool.name: tool for tool in tools}

"""Tools the PR merge monitor agent can invoke."""

from __future__ import annotations

import json
import subprocess
from abc import ABC, abstractmethod
from clients import MonitorConfig, DockerClient, GitHubClient, ensure_logs_dir
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar


def _blocked_workflow_change(path: str, config: MonitorConfig) -> bool:
    """
    Return True if writing to `path` should be blocked by guardrails.
    CI workflow files under .github/workflows/ can significantly alter how the repository's automation behaves,
    so edits to them are blocked by default and must be explicitly permitted via the allow_ci_workflow_changes guardrail in the config.
    Normalises backslashes to forward slashes before checking so the guard is compatible for Windows paths too.
 
    Args:
        path: Relative file path the agent wants to write, as supplied by the LLM to WriteFileTool.
        config: Active monitor config, used to read the guardrails dict.
 
    Returns:
        True if the write should be blocked.
        False if the write is permitted (either path is not a workflow file or the guardrail is explicitly enabled). 
    """
    if config.guardrails.get("allow_ci_workflow_changes"):
        return False

    normalized = path.replace("\\", "/")
    return normalized.startswith(".github/workflows/")

def build_tool_registry(
    config: MonitorConfig,
    *,
    cwd: Path | None = None,
    use_docker: bool = False,
    docker_image: str = "node:20-bookworm",
) -> dict[str, Tool]:
    """
    Instantiate all tools and return them as a name-keyed registry dict.
    This is the single place where tools are wired together with their dependencies.
    Agent.__init__ calls this once and stores the result in self.tools, which is then used both to dispatch
    LLM tool-use requests (_dispatch_tool) and to generate the tool definitions sent to the API (as_anthropic_tool).
    The four core tools (PRStatusTool, PRCommentsTool, ShellCommandTool, WriteFileTool) are always included.
    DockerExecTool is appended only when use_docker=True so that the LLM is never offered a tool it cannot use.
 
    Args:
        config: Active monitor config forwarded to each tool that needs repo/PR details or guardrail access.
        cwd: Working directory forwarded to tools that run shell commands or write files. Defaults to the process CWD.
        use_docker: When True, adds DockerExecTool backed by a freshly initialised DockerClient.
                The DockerClient constructor will raise if the daemon is unreachable.
        docker_image: Docker image passed to DockerClient when use_docker=True. Defaults to node:20-bookworm; override for non-JS repos.
 
    Returns:
        Dict mapping each tool's name string to its Tool instance, ready for use as Agent.tools.
    """
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


@dataclass
class ToolResult:
    """
    Uniform return type for every Tool.run() call.
    Wrapping all outcomes in a single type results in the LLM loop in agent.py never needs to catch exceptions from tools.
    Failures are always represented as a ToolResult with ok=False rather than a raised exception.
 
    Attributes:
        ok: True if the tool completed successfully, False otherwise.
        output: Human/LLM-readable string describing the outcome.
                On success this is the tool's payload; on failure it explains what went wrong.
                Always present so the LLM can reason about the result.
        data: Optional structured payload for tools that return machine-readable data (e.g. PRStatusTool).
                None for action-only tools like ShellCommandTool and WriteFileTool.
    """
    ok: bool
    output: str
    data: dict[str, Any] | None = None


class Tool(ABC):
    """
    Abstract base class for all agent tools.
    Each concrete subclass represents one capability the LLM can invoke.
    Subclasses must declare class-level `name` and `description` strings
    (used to register the tool and populate the Anthropic tool definition) and implement `schema()` and `run()`.
 
    Class variables:
        name: Unique snake_case identifier for this tool.
                Used as dict key in the tool registry and matched against the `name` field in the LLM's tool_use response blocks.
        description: One-or-two sentence description sent to the LLM so it knows when and how to invoke this tool.
    """
    name: ClassVar[str]
    description: ClassVar[str]

    @abstractmethod
    def schema(self) -> dict[str, Any]:
        """
        Return the JSON Schema describing this tool's input parameters.
 
        The returned dict is placed under the `input_schema` key in the Anthropic tool definition and
        tells the LLM what arguments to supply when invoking the tool.
        Tools that take no arguments should return a schema with an empty `properties` dict.
        """

    @abstractmethod
    def run(self, **kwargs: Any) -> ToolResult:
        """
        Execute the tool with the provided keyword arguments and return a result.
        kwargs will match the properties declared in schema().
        Implementations must never raise errors and should be caught and returned as a
        ToolResult with ok=False so the LLM can read the error and decide how to proceed.
        """

    def as_anthropic_tool(self) -> dict[str, Any]:
        """
        Serialise this tool into the dict format as expected by the Anthropic API.
        Combines the class-level name and description with the instance's schema() output into
        the structure required by the `tools` parameter of anthropic.messages.create().
        Called once per tool per LLM request in Agent._run_llm_loop.
 
        Returns:
            Dict with name, description and input_schema keys.
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.schema(),
        }


class PRStatusTool(Tool):
    """Fetches and summarises the current merge-readiness of the configured PR."""

    name = "get_pr_status"
    description = "Fetch CI, merge state, and merge-readiness for the configured PR."

    def __init__(self, github: GitHubClient, config: MonitorConfig) -> None:
        """
        Args:
            github: Initialised GitHubClient used to call fetch_pr().
            config: Active monitor config, used to populate repo/PR metadata in the summary dict.
        """
        self.github = github
        self.config = config

    def schema(self) -> dict[str, Any]:
        """This tool takes no input arguments. The PR to check is determined entirely by the active config."""
        return {"type": "object", "properties": {}, "required": []}

    def run(self, **_kwargs: Any) -> ToolResult:
        """
        Fetch PR data, derive CI and merge state, and write a status snapshot.
        Classifies each status check as failing, pending or passing based on GitHub's conclusion/status strings.
        Derives a merge_ready boolean and a list of blocked_reasons strings from the raw API data so
        the LLM and agent logic have a clean, normalised view rather than raw GitHub enums.
        Writes the full summary to logs/latest-status.json so operators can inspect the last observed state without re-running the agent.

        Failing conclusions recognised:
            - failure
            - cancelled
            - timed_out
            - action_required
        
        Pending statuses recognised:
            - queued
            - in_progress
            - pending
 
        Blocked reasons appended (each only when applicable):
            branch_behind_base: merge_state is BEHIND
            merge_conflicts: merge_state is DIRTY
            ci_failing: one or more checks have a failing conclusion
            ci_pending: one or more checks are still running
            changes_requested: review_decision is CHANGES_REQUESTED
            review_required: review_decision is REVIEW_REQUIRED
 
        Returns:
            ToolResult with ok=True, summary JSON as output and summary dict as data for use by Agent.observe().
        """
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
    """Fetches unresolved review-comment threads for the configured PR."""

    name = "get_pr_comments"
    description = "Fetch unresolved review threads for the configured PR."

    def __init__(self, github: GitHubClient) -> None:
        """
        Args:
            github: Initialised GitHubClient used to call fetch_unresolved_threads().
        """
        self.github = github

    def schema(self) -> dict[str, Any]:
        """
        This tool takes no input arguments.
        The PR to check is determined entirely by the active config held by the GitHubClient.
        """
        return {"type": "object", "properties": {}, "required": []}

    def run(self, **_kwargs: Any) -> ToolResult:
        """
        Retrieve unresolved review threads and write a snapshot to disk.
        Delegates to GitHubClient.fetch_unresolved_threads() for the API call.
        Wraps the result in a small envelope dict including thread count.
        The LLM can judge how much reviewer feedback remains without counting list items.
        Writes payload to logs/latest-comments.json so operators can inspect last observed review state without re-running the agent.
 
        Returns:
            ToolResult with ok=True, the payload JSON as output and the payload dict as data for use by Agent.observe().
        """
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
    """Runs arbitrary shell commands in the target repository directory."""

    name = "run_shell_command"
    description = (
        "Run a shell command in the target repo (tests, lint, git operations). "
        "Blocked for .github/workflows unless explicitly allowed in config."
    )

    def __init__(self, config: MonitorConfig, cwd: Path | None = None) -> None:
        """
        Args:
            config: Active monitor config, used to evaluate guardrails before executing the command.
            cwd: Working directory in which commands are executed.
                    Defaults to the current process directory, which should be the root of the target repository.
        """
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

    def __init__(self, config: MonitorConfig, cwd: Path | None = None) -> None:
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

"""External service clients for the PR merge monitor agent."""

from __future__ import annotations

import json
import os
import subprocess
import yaml
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Absolute path to the directory containing this file, used as the project root so that all other paths
# (config, logs, prompts) resolve correctly regardless of the working directory the process was launched from.
ROOT_DIR = Path(__file__).resolve().parent

# Default config file the agent loads when no explicit path is provided.
# Users are expected to fill in config/active.yaml with their repo and PR details before running the agent.
DEFAULT_CONFIG = ROOT_DIR / "config" / "active.yaml"

# Directory where the agent writes its JSONL run log.
# Created on-demand by ensure_logs_dir() rather than at import time so module imports never has fs side effects.
LOGS_DIR = ROOT_DIR / "logs"


def shutil_which(cmd: str) -> str | None:
    """
    Look up a command on the system PATH and return its absolute path.
    A thin wrapper around shutil.which that defers the import, keeping it available to the
    module-level guard in GitHubClient and DockerClient without adding a top-level import.
    Returns None (rather than raising) when the command isn't present, making call-sites read as boolean checks.
 
    Args:
        cmd: The binary name to search for (e.g. "gh", "docker").
 
    Returns:
        Absolute path string if found on PATH, otherwise None.
    """
    from shutil import which
    return which(cmd)

def ensure_logs_dir() -> Path:
    """
    Create the logs directory if it doesn't already exist and return its path.
    Uses mkdir(parents=True, exist_ok=True) so it's safe to call repeatedly and will create any missing parent directories.
    Called by Agent._record before every log write rather than once at startup
    so the directory is only created when the agent actually produces output.
 
    Returns:
        The absolute path to the logs directory (LOGS_DIR).
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR


@dataclass
class MonitorConfig:
    """
    Validated, typed representation of the active.yaml configuration file.
 
    Attributes:
        repo: GitHub repository in "owner/name" format (e.g. "radiusxt/backend-api").
        pr: Pull-request number to monitor.
        base_branch: The branch to monitor for PRs. Defaults to "main".
        guardrails: Free-form dict of agent behaviour constraints loaded from the "guardrails" YAML key (e.g. allow_unrelated_changes).
        loop: Polling / scheduling settings loaded from the "loop" YAML key (e.g. mode, interval, ci_poll_seconds).
        notifications: Alerting settings loaded from the "notifications" YAML key (e.g. Slack webhook, email recipients).
    """
    repo: str
    pr: int
    base_branch: str
    guardrails: dict[str, Any]
    loop: dict[str, Any]
    notifications: dict[str, Any]

    @classmethod
    def load(cls, path: Path | None = None) -> MonitorConfig:
        """
        Read, validate and return a MonitorConfig from a YAML file.
        Falls back to DEFAULT_CONFIG when no path is supplied.
        Performs two sanity checks before constructing the dataclass.
        The repo field must be present and the PR number must be a positive integer.
        All other keys are optional and fall back to sensible defaults.
 
        Args:
            path: Explicit path to a YAML config file, or None to use DEFAULT_CONFIG.
 
        Returns:
            A fully populated MonitorConfig instance.
 
        Raises:
            FileNotFoundError: If the resolved config path does not exist.
            ValueError: If repo is missing/placeholder or PR is not a positive integer.
        """
        config_path = path or DEFAULT_CONFIG

        if not config_path.exists():
            raise FileNotFoundError(f"Missing {config_path}. Copy config/example.yaml to config/active.yaml.")
        
        raw = yaml.safe_load(config_path.read_text()) or {}
        repo = raw.get("repo", "")
        pr = int(raw.get("pr") or 0)

        if not repo or repo == "owner/repo":
            raise ValueError("Set repo in config/active.yaml")

        if pr <= 0:
            raise ValueError("Set pr number in config/active.yaml")

        return cls(
            repo=repo,
            pr=pr,
            base_branch=raw.get("base_branch", "main"),
            guardrails=raw.get("guardrails", {}),
            loop=raw.get("loop", {}),
            notifications=raw.get("notifications", {}),
        )


class GitHubClient:
    """Wraps the GitHub CLI (gh) for PR status and review data."""

    def __init__(self, config: MonitorConfig) -> None:
        """
        Store config and verify the gh CLI is installed and authenticated.
 
        Args:
            config: Parsed monitor configuration supplying repo and PR details.
 
        Raises:
            RuntimeError: If gh is not on PATH or is not authenticated.
        """
        self.config = config
        self._ensure_gh()

    def _ensure_gh(self) -> None:
        """
        Verify that the gh CLI binary exists and has an active auth session.
        Called once during __init__ so any environment problems surface immediately rather than on the first API call mid-run.
 
        Raises:
            RuntimeError: If gh is not found on PATH, or if `gh auth status` returns a non-zero exit code,
                    this indicaties the user is not logged in.
        """
        if not shutil_which("gh"):
            raise RuntimeError("gh CLI is required. Install: https://cli.github.com/")

        result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)

        if result.returncode != 0:
            raise RuntimeError("gh is not authenticated. Run: gh auth login")

    def _run(self, args: list[str]) -> str:
        """
        Execute an arbitrary gh CLI command and return its stdout as a string.
        A thin wrapper around subprocess.run that centralises error handling.
        Any non-zero exit code raises RuntimeError with the CLI's own error
        message so callers never need to inspect return codes themselves.
 
        Args:
            args: Full argv list starting with "gh" (e.g. ["gh", "pr", "view", "42", ...]).
 
        Returns:
            The command's stdout text on success.
 
        Raises:
            RuntimeError: If the command exits with a non-zero code, with the CLI's stderr as the message.
        """
        result = subprocess.run(args, capture_output=True, text=True)

        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())

        return result.stdout

    def fetch_pr(self) -> dict[str, Any]:
        """
        Fetch core PR metadata from the GitHub REST API via the gh CLI.
        Retrieves a fixed set of fields covering everything the agent needs to  assess merge-readiness.
        These parameters include;
            - state
            - mergable flag
            - status
            - branch names
            - CI status-check rollup
            - review decisions and commits.
 
        Returns:
            Parsed JSON dict matching the shape returned by `gh pr view --json <fields>`.
 
        Raises:
            RuntimeError: If the gh CLI call fails (e.g. PR not found, no network access, insufficient permissions).
        """
        payload = self._run(
            [
                "gh",
                "pr",
                "view",
                str(self.config.pr),
                "--repo",
                self.config.repo,
                "--json",
                "number,title,url,state,mergeable,mergeStateStatus,baseRefName,"
                "headRefName,statusCheckRollup,reviews,reviewDecision,commits",
            ]
        )

        return json.loads(payload)

    def fetch_unresolved_threads(self) -> list[dict[str, Any]]:
        """
        Return all unresolved review-comment threads on the PR via GraphQL.
        Uses the GitHub GraphQL API (via `gh api graphql`) to fetch up to 100 review threads.
        Threads marked as resolved are filtered out.
        For each remaining thread, only the first comment is surfaced to keep the payload small
        while still providing enough context for the LLM to act on.
        The body of each comment is truncated to 500 characters to prevent large comments from blowing out the LLM context window.
 
        Returns:
            List of dicts, one per unresolved thread, each with keys:
                author: GitHub login of the comment author, or None.
                path: File path the comment is attached to.
                url: Direct URL to the comment on GitHub.
                body: First 500 chars of the first comment's body.
                created_at: ISO 8601 timestamp of the first comment.
                comment_count: Total number of replies in the thread.
 
        Raises:
            RuntimeError: If the gh CLI call fails.
            ValueError: Implicitly, if self.config.repo is not in "owner/name" format (split will raise).
        """
        owner, name = self.config.repo.split("/", 1)
        query = """
            query($owner: String!, $name: String!, $number: Int!) {
            repository(owner: $owner, name: $name) {
                pullRequest(number: $number) {
                reviewThreads(first: 100) {
                    nodes {
                    isResolved
                    comments(first: 10) {
                        nodes {
                        author { login }
                        body
                        path
                        url
                        createdAt
                        }
                    }
                    }
                }
                }
            }
            }
        """
        payload = self._run([
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={self.config.pr}",
        ])
        data = json.loads(payload)
        unresolved: list[dict[str, Any]] = []
        threads = (
            data.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
            .get("nodes", [])
        )
        
        for thread in threads:
            if thread.get("isResolved"):
                continue

            comments = thread.get("comments", {}).get("nodes", [])

            if not comments:
                continue

            first = comments[0]
            unresolved.append(
                {
                    "author": (first.get("author") or {}).get("login"),
                    "path": first.get("path"),
                    "url": first.get("url"),
                    "body": (first.get("body") or "")[:500],
                    "created_at": first.get("createdAt"),
                    "comment_count": len(comments),
                }
            )

        return unresolved


class AnthropicClient:
    """LLM client for agent reasoning and tool selection."""

    def __init__(self, model: str = "claude-sonnet-4-6") -> None:
        """
        Initialises the Anthropic SDK client.
        Retrieves API key from .env and imports the anthropic package lazily so the codebase 
        can be imported and tested in environments where the package is not installed.
        Defaults to Claude Sonnet to balance capability and cost for the tool-calling loop.
 
        Args:
            model: The Anthropic model identifier to use for all completions.
 
        Raises:
            RuntimeError: If ANTHROPIC_API_KEY is not set, or if the anthropic package is not installed.
        """
        api_key = os.environ.get("ANTHROPIC_API_KEY")

        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")

        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError("Install dependencies: pip install -r requirements.txt") from exc

        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    @property
    def available(self) -> bool:
        return True

    def complete_with_tools(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 4096,
    ) -> Any:
        """
        Sends a messages request to the Anthropic API with tool definitions.
        A thin pass-through to the SDK's messages.create method, exposing only the parameters the agent loop needs.
        The keyword-only signature prevents positional argument mistakes when called from _run_llm_loop.
 
        Args:
            system: System prompt text sent with every request.
            messages: Full conversation history in Anthropic's message format, including any prior tool-use and tool-result turns.
            tools: List of tool definition dicts in Anthropic tool schema format, as returned by Tool.as_anthropic_tool().
            max_tokens: Maximum tokens the model may generate in this response.
                    Defaults to 4096, enough for verbose tool-call reasoning.
 
        Returns:
            The raw anthropic.types.Message object, which the caller inspects for text and tool_use content blocks.
        """
        return self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=tools,
        )


class DockerClient:
    """Optional Docker client for running checks in an isolated container."""

    def __init__(self, image: str = "node:20-bookworm") -> None:
        """
        Verify Docker is available and stores the target image name.
 
        Args:
            image: Docker image to use for all container runs. Defaults to node:20-bookworm, which suits JS/TS projects.
                    Override for Python or other runtimes.
 
        Raises:
            RuntimeError: If the docker binary is not on PATH, or if the Docker daemon isn't reachable.
        """
        if not shutil_which("docker"):
            raise RuntimeError("docker is not installed or not on PATH")

        self.image = image
        self._ensure_daemon()

    def _ensure_daemon(self) -> None:
        """
        Confirm the Docker daemon is running and accepting connections.
 
        Runs `docker info` and raises if it exits non-zero.
        Called once during __init__ so daemon connectivity is verified before spinning up a container.
 
        Raises:
            RuntimeError: If `docker info` fails, typically because the daemon stopped or the user lacks socket permissions.
        """
        result = subprocess.run(["docker", "info"], capture_output=True, text=True)

        if result.returncode != 0:
            raise RuntimeError("Docker daemon is not reachable")

    def run(self, command: str, *, workdir: str = "/workspace", timeout: int = 600) -> str:
        """
        Executes a shell command inside a disposable Docker container.
        Mounts the CWD into a container at `workdir`, runs the command and returns the combined stdout + stderr.
        The container is automatically removed after it exits with --rm.
 
        Args:
            command: Shell command string passed to `bash -lc`.
                    The login shell flag (-l) ensures PATH and shell init files are sourced, which matters for nvm / pyenv style setups.
            workdir: Absolute path inside container where host is mounted and where the command runs. Defaults to "/workspace".
            timeout: Maximum seconds to wait for container to finish before subprocess raises TimeoutExpired. Default is 600s.
 
        Returns:
            Combined stdout and stderr output of the command.
 
        Raises:
            RuntimeError: Includes combined output if the command exits with a non-zero return code.
            subprocess.TimeoutExpired: If the container runs longer than `timeout` seconds.
        """
        args = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{os.getcwd()}:{workdir}",
            "-w",
            workdir,
            self.image,
            "bash",
            "-lc",
            command,
        ]
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        output = (result.stdout or "") + (result.stderr or "")

        if result.returncode != 0:
            raise RuntimeError(f"docker command failed ({result.returncode}):\n{output}")

        return output

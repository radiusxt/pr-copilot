"""External service clients for the PR merge monitor agent."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT_DIR / "config" / "active.yaml"
LOGS_DIR = ROOT_DIR / "logs"


@dataclass
class MonitorConfig:
    repo: str
    pr: int
    base_branch: str
    guardrails: dict[str, Any]
    loop: dict[str, Any]
    notifications: dict[str, Any]

    @classmethod
    def load(cls, path: Path | None = None) -> MonitorConfig:
        config_path = path or DEFAULT_CONFIG
        if not config_path.exists():
            raise FileNotFoundError(
                f"Missing {config_path}. Copy config/example.yaml to config/active.yaml."
            )
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
        self.config = config
        self._ensure_gh()

    def _ensure_gh(self) -> None:
        if not shutil_which("gh"):
            raise RuntimeError("gh CLI is required. Install: https://cli.github.com/")
        result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError("gh is not authenticated. Run: gh auth login")

    def _run(self, args: list[str]) -> str:
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result.stdout

    def fetch_pr(self) -> dict[str, Any]:
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
        payload = self._run(
            [
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
            ]
        )
        data = json.loads(payload)
        threads = (
            data.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
            .get("nodes", [])
        )
        unresolved: list[dict[str, Any]] = []
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
    """Optional LLM client for agent reasoning and tool selection."""

    def __init__(self, model: str = "claude-sonnet-4-20250514") -> None:
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
        if not shutil_which("docker"):
            raise RuntimeError("docker is not installed or not on PATH")
        self.image = image
        self._ensure_daemon()

    def _ensure_daemon(self) -> None:
        result = subprocess.run(["docker", "info"], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError("Docker daemon is not reachable")

    def run(self, command: str, *, workdir: str = "/workspace", timeout: int = 600) -> str:
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


def shutil_which(cmd: str) -> str | None:
    from shutil import which

    return which(cmd)


def ensure_logs_dir() -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return LOGS_DIR

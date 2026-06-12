#!/usr/bin/env python3
"""Terminal UI for the PR merge monitor agent."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent import Agent
from clients import MonitorConfig, ROOT_DIR, ensure_logs_dir


def _load_config(args: argparse.Namespace) -> MonitorConfig:
    return MonitorConfig.load(args.config)


def cmd_status(args: argparse.Namespace) -> int:
    config = _load_config(args)
    agent = Agent(config, use_llm=False)
    status, comments = agent.observe()
    print(json.dumps({"status": status, "comments": comments}, indent=2))
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    config = _load_config(args)
    agent = Agent(
        config,
        cwd=Path(args.cwd).resolve() if args.cwd else Path.cwd(),
        use_llm=not args.no_llm,
        use_docker=args.docker,
    )
    result = agent.run_once()
    agent._print_result(result)
    return 0 if result.status != "blocked" else 2


def cmd_loop(args: argparse.Namespace) -> int:
    config = _load_config(args)
    agent = Agent(
        config,
        cwd=Path(args.cwd).resolve() if args.cwd else Path.cwd(),
        use_llm=not args.no_llm,
        use_docker=args.docker,
    )
    agent.run_loop(mode=args.mode, max_iterations=args.max_iterations)
    return 0


def cmd_repl(args: argparse.Namespace) -> int:
    config = _load_config(args)
    agent = Agent(
        config,
        cwd=Path(args.cwd).resolve() if args.cwd else Path.cwd(),
        use_llm=not args.no_llm,
        use_docker=args.docker,
    )

    print("PR monitor REPL. Commands: status | once | loop | quit")
    while True:
        try:
            line = input("monitor> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in {"quit", "exit", "q"}:
            break
        if line == "status":
            status, comments = agent.observe()
            print(json.dumps({"status": status, "comments": comments}, indent=2))
            continue
        if line == "once":
            agent._print_result(agent.run_once())
            continue
        if line.startswith("loop"):
            parts = line.split()
            mode = parts[1] if len(parts) > 1 else None
            agent.run_loop(mode=mode, max_iterations=args.max_iterations)
            continue
        print("Unknown command. Try: status | once | loop [fixed|dynamic] | quit")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PR merge monitor agent")
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT_DIR / "config" / "active.yaml",
        help="Path to active config",
    )
    parser.add_argument("--cwd", help="Target application repo working directory")
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Observation-only mode (no Anthropic API calls)",
    )
    parser.add_argument(
        "--docker",
        action="store_true",
        help="Register docker_exec tool (requires Docker)",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Stop loop after N iterations",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Fetch PR status and comments once")

    once = sub.add_parser("once", help="Run one monitor iteration")
    once.set_defaults(func=cmd_once)

    loop = sub.add_parser("loop", help="Run until merge-ready or blocked")
    loop.add_argument("--mode", choices=["fixed", "dynamic"], default=None)
    loop.set_defaults(func=cmd_loop)

    repl = sub.add_parser("repl", help="Interactive terminal session")
    repl.set_defaults(func=cmd_repl)

    return parser


def main() -> int:
    ensure_logs_dir()
    parser = build_parser()
    args = parser.parse_args()

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

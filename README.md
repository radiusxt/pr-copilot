# PR Copilot

A minimal, iterable internal tool for keeping a pull request merge-ready: green CI, resolved review threads and no merge conflicts for delivering better software faster. 

`agent.py` orchestrates the loop, `tools.py` defines agent capabilities, `clients.py` talks to GitHub, Anthropic and/or Docker and `ui.py` wraps the tool around a CLI.

## Installation

### Prerequisites

- [GitHub CLI](https://cli.github.com/) (`gh`) authenticated: `gh auth login`
- Python 3.11+
- Optional: `ANTHROPIC_API_KEY` for LLM
- Optional: Docker for isolated sandbox environment for test runs
- A target repo with an open PR

### 1. Configure Agent Capabilities & Install Dependencies

```bash
pip install -r requirements.txt
cp config/example.yaml config/active.yaml
```

Edit `config/active.yaml` with your repository name, PR number and guardrail preferences.

### 2. Check PR Status

```bash
python3 ui.py status
```

Fetches the current PR state via `gh` and prints a JSON summary. No LLM call is made. Writes `logs/latest-status.json` and `logs/latest-comments.json`.

### 3. Add Anthropic API Key for LLM

```bash
export ANTHROPIC_API_KEY=...

OR

echo "ANTHROPIC_API_KEY=..." >> .env
```

### 4. Running the Agent

#### Single Iteration

```bash
# With Claude
python3 ui.py once --cwd /path/to/target/repo

# Observation only
python3 ui.py once --no-llm
```

#### Continuous Loop

```bash
# Fixed interval from config/active.yaml (loop.interval)
python3 ui.py loop --mode fixed

# Dynamic — poll until CI/merge state changes
python3 ui.py loop --mode dynamic
```

#### Interactive REPL

```bash
python3 ui.py repl
```

Commands: `status`, `once`, `loop fixed`, `loop dynamic`, `quit`

## Safeguards & Agent Restrictions

When CI fails for reasons outside the PR's diff, the agent stops and reports it. Merging the latest `base_branch` first is often the fix or another PR may have already resolved the issue on main. The agent may attempt to find methods to resolve issues without solving the actual issue itself. Safeguards are designed to prevent this so the agent will not, by default:

- Edit `.github/workflows` to force failing checks and tests to pass
- Make changes outside the PR's diff scope 
- `git push` without `auto_push: true` in config or explicit user approval that allows it to.
- Merge the PR into `main` or a branch without `auto_merge: true` in config

In `config/active.yml`, you can increase/decrease the amount of autonomy the agent has over a PR.

## References

- [A loop is all you need](https://dev.to/adgapar/a-loop-is-all-you-need-building-conversation-ai-agents-1039)
- [Agentic Loop Example](https://github.com/bitswired/demos/tree/main/projects/agentic-loop)

# PR Babysit — one iteration

You are keeping a pull request merge-ready. Work in this repo's working tree (the target application repo if configured separately).

## Inputs

Read these before acting:

1. `logs/latest-status.json` — CI, merge state, review decision
2. `logs/latest-comments.json` — unresolved review threads
3. `config/active.yaml` — guardrails

## Priority order

1. **Merge conflicts / branch behind base** — merge or rebase `base_branch` into the PR branch. If intents conflict, stop and ask the human.
2. **CI failures in PR scope** — fix failing tests, lint, or type errors caused by this PR's changes. Do not edit `.github/workflows` or unrelated code to force green.
3. **Review comments** — address valid unresolved threads. Skip resolved threads. Disagree politely when a comment is wrong.

## Guardrails (from config)

- Respect `max_files_per_fix`
- Never change CI workflows unless `allow_ci_workflow_changes: true`
- Never make unrelated changes unless `allow_unrelated_changes: true`
- Only `git push` when `auto_push: true` or the user explicitly approves

## After each fix

1. Run relevant local checks (tests, lint) before pushing
2. Re-run `./scripts/pr-status.sh` to refresh state
3. If still blocked, explain what remains and whether human input is needed

## Stop conditions

- **Success:** `merge_ready: true` in latest status — report ready to merge
- **Blocked:** architectural disagreement, failing CI outside PR scope, or guardrail would be violated — report clearly and stop

## Output format

End with a short status block:

```
Status: merge-ready | in-progress | blocked
Actions: <what you did this iteration>
Next: <what happens on the next loop tick>
Human needed: yes/no — <reason if yes>
```

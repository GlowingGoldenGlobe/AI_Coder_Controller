# PHI‑4 Commit Tuning Guide

This guide instructs the PHI‑4 (via Copilot Chat) how to plan and execute high‑quality commits safely in this project.

## Goals
- Produce small, atomic commits with clear intent.
- Use Conventional Commits for messages.
- Validate changes (lint/tests) before committing when possible.
- Preserve safety: never commit directly to `main`; prefer a feature branch.

## Principles
- Atomic changes: one logical change per commit (code + tests + docs for that change).
- Message clarity: start with type/scope, present‑tense subject, include rationale, list noteworthy impacts.
- Reproducible: commits should pass basic checks and not leave the repo broken.
- Traceability: link related docs/notes or objectives where relevant.

## Message Format (Conventional Commits)
- Template:
  
  ```
  <type>(<scope>): <subject>
  
  <body>
  
  <footers>
  ```

- Types: `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `chore`.
- Scope: module or area, e.g., `vsbridge`, `policy`, `ui`, `ocr`.
- Subject: imperative mood, no trailing period. E.g., `feat(ui): add scroll steps control`.
- Body: what/why details, risks, follow‑ups.
- Footers: `BREAKING CHANGE:`, references, issue IDs if any.

## Workflow (Copilot‑assisted)
1. Plan
   - Summarize intended diff: files, functions, public APIs, tests/docs to update.
   - Confirm commit scope is atomic; split if not.
2. Stage
   - In VS Code terminal, run:
     ```
    # If this is a venv project, activate first (Agent requirement):
    Scripts/activate
     git status
     git switch -c feat/<short-scope>  # if not on a feature branch
     git add -p                        # stage only intended hunks
     ```
3. Validate
   - If available, run:
     ```
     python -m pip install -r requirements.txt
     # run unit tests or smoke scripts
     python scripts/ocr_smoke_test.py
     ```
   - Optional lint/format:
     ```
     # placeholder; add tools if adopted
     ```
4. Commit
   - Generate message using the template and staged changes summary.
   - Example:
     ```
     git commit -m "feat(ui): add scroll steps control" -m "Adds Spinbox for steps and wires handlers in main. Logs steps to JSONL."
     ```
5. Push & PR (optional)
   - ```
     git push -u origin HEAD
     ```
   - Open a PR; summarize the change and testing steps.

## Safety & Guardrails
- Never commit to `main` directly; use branches.
- Show a concise diff summary to user for approval when changes are non‑trivial:
  - Files changed, additions/deletions, key functions touched.
- Avoid committing secrets or local paths; scan staged hunks before commit.

## Using the Controller
- To run terminal commands from objectives:
  - Add lines like:
    - `terminal: git status`
    - `terminal: git add -p`
    - `terminal: git commit -m "fix(vsbridge): handle None pyautogui"`
- Or click UI → Focus Terminal and type commands manually.

## Workflow Status Notes (PASS/FAIL/DEFERRED)

Some automation runs intentionally avoid interactive UI actions when Agent Mode is active. In that case, the workflow summary may report:

- `DEFERRED`: interactive steps were queued for later execution (a successful run, but not “full coverage”)

When you need to execute the deferred queue, ensure it is safe:
- Agent Mode OFF for the process (can be forced with `AI_CONTROLLER_AGENT_MODE=0`)
- Controls are not paused in `config/controls_state.json`
- No unexpected controls owner is active (or clear it if stale)

Tip: use the VS Code tasks "Controls: Unpause" and "Run Deferred Workflow Actions (Agent Mode OFF, max=1)" for a safe, bounded live run.

### Workflow Deferral & Resume Tuning

These knobs let you control whether the workflow performs UI automation now or queues it for later.

**Environment variables (per-process)**

- `AI_CONTROLLER_AGENT_MODE`:
  - `1`/`true` → treat this process as Agent Mode active
  - `0`/`false` → treat this process as Agent Mode inactive
- `AI_CONTROLLER_DEFER_INTERACTIONS_WHEN_AGENT_MODE`:
  - `1` → defer interactive steps when Agent Mode is active
  - `0` → allow interactive steps even if Agent Mode is active (use with caution)
- `AI_CONTROLLER_ENABLE_VERIFY_PHASE`:
  - `1` → enable the navigation verify phase
  - `0` → disable the verify phase (useful when debugging flakiness)

**Controls state**

- `config/controls_state.json`:
  - `paused=true` will force workflows into passive-only / deferred behavior.
  - `owner` blocks other workflows from sending input unless they own controls.

**Deferred queue runner**

- Queue file: `logs/actions/deferred_workflow_actions.jsonl`
- Safe review/run sequence:
  - `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --list`
  - `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --dry-run`
  - Prefer scoping to a workflow run id (recommended for repeatable actions):
    - `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --list --run-id <run_id>`
    - `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --dry-run --run-id <run_id>`
  - `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --live --max 1`
  - (recommended) run a specific action for a specific run:
    - `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --dry-run --run-id <run_id> --id <action_id> --max 1`
    - `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --live --run-id <run_id> --id <action_id> --max 1`
  - Optional cleanup: `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --prune`

**Where to look first**

- `Copilot_Attachments/ContextPack_Current.md` includes a “Runtime state (safety)” section that prints current `paused/owner` and `agent_mode`, plus the recommended resume commands.

## Commit Examples
- Feature:
  - `feat(policy): map 'terminal:' objective to VS Code terminal`
- Fix:
  - `fix(ocr): honor chat_settle_ms for capture timing`
- Docs:
  - `docs: add VSCode Agent integration guide`

## Copilot Prompt (for PHI‑4)
- Use this when preparing a commit message:
  
  """
  You are assisting with commit preparation. Generate a Conventional Commit message for the staged changes.
  Include:
  - type(scope): subject in present tense
  - A body with what/why, risks, and user‑visible effects
  - Optional footers for breaking changes or references
  Keep subject <= 72 chars, wrap body at ~100 chars.
  """


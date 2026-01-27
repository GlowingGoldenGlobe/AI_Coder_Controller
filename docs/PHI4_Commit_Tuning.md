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


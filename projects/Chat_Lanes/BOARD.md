# Parallel Chat Lanes (VS Code tabs)

Generated/updated: 2026-01-28 06:00:44

## Goal

Use multiple VS Code Copilot Chat *tabs/conversations* in the same window to work in parallel.
Each chat lane writes its progress to a lane file so other lanes can read it without needing UI automation.

## How to use (manual, low-friction)

1) Open Copilot Chat in VS Code.
2) Create/open one conversation per lane (e.g., Primary / Workflow / OCR / Triage).
3) In each lane conversation, paste the corresponding lane file contents (or attach it if your Copilot supports attachments).
4) When you finish a step, append a short update to that lane file under the "Notes" section.
5) Periodically skim `notifications.jsonl` to see what other lanes changed.

## Files

- `notifications.jsonl`: append-only event log (workflow scripts write here)
- `lane_<name>.md`: per-lane working memory and handoff notes

## Safety

- This system does not click/type in the UI.
- It does not auto-clean or prune logs; pruning is always manual.
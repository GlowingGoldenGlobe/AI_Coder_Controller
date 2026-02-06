# Tuning Guide (Controller + Workflow)

This project has tuning information split across the README and several docs. This page consolidates the most common knobs.

## Where tuning lives

- Runtime safety + automation timing: `config/policy_rules.json`
- OCR regions and settle delays: `config/ocr.json`
- Orchestrator click/match thresholds: `docs/ORCHESTRATOR_PIPELINES.md` + the pipeline JSON configs under `config/`
- Commit-message tuning: `docs/PHI4_Commit_Tuning.md`

## Core knobs (config/policy_rules.json)

- `vsbridge.delay_ms`, `vsbridge.delay_ms_active`, `vsbridge.delay_ms_release`
  - Raise these if focus/click/type feels racy.
- `vsbridge.dry_run`
  - `true` to prevent effectful UI actions.
- `measurement.threshold`
  - Raise to reduce false-positive template matches; lower if you miss true matches.
- `measurement.retry_attempts`, `measurement.backoff_ms`
  - Raise attempts/backoff for flaky UI.
- `controls.stale_after_s`
  - Controls-state snapshots older than this are treated as stale (less likely to get “stuck blocked”).
- `workflow.defer_interactions_when_agent_mode`
  - When Agent Mode is active, interactive workflow steps are queued instead of executed.
- `workflow.enable_verify_phase`
  - Enables the verify rerun step in the workflow.
- `user_activity.*`
  - Pause-on-input thresholds and resume behavior (ESC, mouse distance, popup, auto-resume).
  - See `config/policy_rules.json` → `user_activity`.
- `cleanup.rules[*].retain_seconds`, `cleanup.rules[*].max_keep`
  - Adjust media retention (especially `logs/screens` where `.assessed` markers are required).

## OCR knobs (config/ocr.json)

- `targets.*` (ROIs)
  - Tighten ROIs to reduce noise; widen if you clip the chat region.
- `chat_settle_ms` / `app_settle_ms`
  - Raise if OCR captures too early while UI is still updating.
- `tesseract_cmd`, `tesseract_psm`
  - Ensure Tesseract is correctly installed and configured.

## Workflow outcome semantics

The workflow runner `Scripts/workflow_test_gather_assess.py` emits `logs/tests/workflow_summary_*.json` with:

- `status: PASS | FAIL | DEFERRED`
- `DEFERRED` means interactive steps were queued for later execution (not a PASS, not treated as a failure).

## Agent Mode: automation performance (how to interpret results)

When Agent Mode is active (or controls are paused / owned), the controller may intentionally avoid sending keyboard/mouse input. In that case, “performance” is best measured as:

- Did the workflow correctly *detect* that it was unsafe to act?
- Did it record enough context to resume safely later?
- Did the deferred queue stay bounded (no runaway growth for the same actions)?

**Where to look (per run)**

- Summary artifact: `logs/tests/workflow_summary_<run_id>.json`
  - `status`:
    - `PASS`: interactive steps ran and checks passed.
    - `DEFERRED`: interactive steps were not executed; they were queued for later.
    - `FAIL`: required checks failed.
  - `pass`:
    - `true` means the workflow’s required checks succeeded (including the `DEFERRED` lane).
  - `workflow_info.interaction_context` (if present):
    - `interactions_deferred`: whether any interactive step was deferred.
    - `deferred_count`: how many actions were queued in this run.
    - `defer_reason`: why the run deferred (e.g., Agent Mode / paused / owner).

- Matching operator guidance: `logs/tests/workflow_recommendations_<run_id>.md`
  - Use this as the “handoff” for what to do next (drain queue, unpause, disable verify phase, etc.).

**Queue health (cross-run)**

- Deferred queue file: `logs/actions/deferred_workflow_actions.jsonl`
- Context Pack quick stats: `Copilot_Attachments/ContextPack_Current.md` includes queue counts and “Runtime state (safety)”.
- If you see repeated `DEFERRED` runs with a growing queue, treat that as a signal to:
  - Review with `--list` / `--dry-run` first.
  - Drain in bounded live runs (`--live --max 1`) only when safe.
  - Prune obvious duplicates (`--prune`) if the queue was inflated by repeated runs.

**Common false alarms vs real failures**

- `DEFERRED` is expected when `paused=true` or another workflow owns controls; it is not a failure.
- A `PASS` is the only outcome that implies “full coverage” UI automation occurred.
- If flakiness appears as a failed navigation/focus step:
  - Prefer treating early focus probes as advisory; rely on required steps + verify phase for confidence.
  - If debugging timing/focus issues, temporarily set `AI_CONTROLLER_ENABLE_VERIFY_PHASE=0` to reduce churn.

**Recommended reporting for Agent Mode operators**

For any run you’re handing off, report:

- Run ID (from the filename stem of `workflow_summary_<run_id>.json`)
- `status` + `pass`
- `deferred_count` (if present) and the current queue size
- Current `paused/owner` from `config/controls_state.json`
- Whether Agent Mode is active for this run (process override via `AI_CONTROLLER_AGENT_MODE` if applicable)

## Environment variable overrides (selected)

These override config for a single process invocation:

- `AI_CONTROLLER_AGENT_MODE=0|1` (forces Agent Mode OFF/ON for that process)
- `AI_CONTROLLER_DEFER_INTERACTIONS_WHEN_AGENT_MODE=0|1` (workflow override)
- `AI_CONTROLLER_ENABLE_VERIFY_PHASE=0|1` (workflow override)
- `AI_CONTROLLER_DEFERRED_QUEUE_DEDUPE_WINDOW_S=<seconds>` (set to `0` to disable; any positive value enables dedupe so pending actions are not re-enqueued)
- `AI_CONTROLLER_DEFERRED_QUEUE_DONE_COOLDOWN_S=<seconds>` (suppresses re-enqueue of an action id shortly after it was completed)
- `AI_CONTROLLER_ENABLE_COPILOT_APP_INTERACTION=0|1`
- `AI_CONTROLLER_RUN_CLEANUP=0|1`

## Practical recipes

- Inspect deferred actions (safe; no execution):
  - `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --list --max 20`
  - `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --dry-run --max 5`
  - Prefer scoping to a run id (recommended for repeatable actions):
    - `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --list --run-id <run_id> --max 20`
    - `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --dry-run --run-id <run_id> --max 5`
  - Target a single action id:
    - `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --dry-run --id <action_id> --max 1`
    - (recommended) scope to a run id:
      - `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --dry-run --run-id <run_id> --id <action_id> --max 1`

- Drain deferred actions safely (bounded):
  - Ensure `config/controls_state.json` has `paused=false` and is fresh (not stale)
    - If `Scripts/controls_inspect.py` reports stale, refresh the snapshot first: `Scripts/python.exe Scripts/controls_set_paused.py --paused false`
  - Run: `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --live --max 1`
  - Prefer scoping to a run id:
    - `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --live --run-id <run_id> --max 1`
  - Or run just one action id:
    - `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --live --id <action_id> --max 1`
    - (recommended) scope to a run id:
      - `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --live --run-id <run_id> --id <action_id> --max 1`
  - Prefer the VS Code tasks:
    - "Controls: Unpause (paused=false)"
    - "Run Deferred Workflow Actions (Agent Mode OFF, max=1)"
    - "Controls: Pause (paused=true)"

- Windows PowerShell: drain exactly 1, then re-pause (safe pattern)
  - This forces Agent Mode OFF only for the deferred runner process and immediately re-pauses controls after one action:
    - `Scripts/python.exe Scripts/controls_set_paused.py --paused false`
    - `$env:AI_CONTROLLER_AGENT_MODE='0'; Scripts/python.exe Scripts/run_deferred_workflow_actions.py --live --max 1; Remove-Item Env:AI_CONTROLLER_AGENT_MODE -ErrorAction SilentlyContinue`
    - `Scripts/python.exe Scripts/controls_set_paused.py --paused true`

- Reduce repeated deferred entries:
  - Run: `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --prune`

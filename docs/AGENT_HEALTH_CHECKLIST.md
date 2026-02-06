# Agent Health Checklist

This checklist covers quick, non-disruptive checks you can run before or after workflows to confirm that Agent Mode, OCR, and commit/verify loops look healthy. All of these commands are read-only with respect to UI input (no new clicks/keys).

## 0. Terminal Environment (Agent Requirement)

- For any terminal commands that operate on venv projects, Agent AIs must first run `Scripts/activate` in that terminal.
- Keep using the same terminal session after activation to preserve the venv context.
- If a terminal is not activated, prefer `Scripts/python.exe` for Python commands.

## 1. Controls / Ownership

- Command:
  - python Scripts/controls_inspect.py --stale-seconds 300
- Confirms:
  - config/controls_state.json is readable.
  - Which owner label (if any) currently holds control (for example "agent", "workflow_test", or empty when free).
  - Whether the snapshot is older than your threshold (stale state).

## 2. Config Sanity (JSON)

- Command:
  - python Scripts/config_validate.py
- Confirms:
  - Core JSON configs (config/policy_rules.json, config/ocr.json, config/templates.json, config/vscode_orchestrator.json) parse cleanly.
  - measurement thresholds/retries look reasonable.
  - Image-analysis region_percent values (if set) are within [0,100] percent.
  - Any configured curated templates in config/templates.json actually exist on disk.

## 3. Image Analysis / Template Readiness

- Command:
  - python Scripts/measurement_smoke_test.py
- Confirms:
  - Image analysis is able to capture a frame from the configured region.
  - Any configured templates in config/templates.json (for example chat_input.templates) match as expected.
  - A JSON summary is written under logs/tests/measurement_smoke_*.json with image path, element count, and per-template match flags.

## 4. Commit / Verify Stability

- Command (example for last 12 hours):
  - python Scripts/commit_verify_summary.py --log logs/actions/commit_verify_2plus2.log --log logs/actions/commit_verify_stability.log --hours 12
- Confirms:
  - Logs under logs/actions/commit_verify_*.log are present and readable.
  - Per-log and overall pass/fail counts and success rates are reasonable (for example, mostly passes in recent windows).
  - A JSON summary is written under logs/tests/commit_verify_summary_*.json.

## 5. When to Pause

- If controls_inspect shows an unexpected long-lived owner or a clearly stale state, pause workflows and clear/fix controls_state.json before running new automation.
- If measurement_smoke_test fails or shows no matching templates, adjust config/ocr.json and config/templates.json before trusting OCR-driven navigation.
- If commit_verify_summary shows many recent failures, investigate the underlying logs in logs/actions/ and logs/tests/ before assuming Agent Mode is healthy.

These checks are designed to be safe to run while Agent Mode is idle or other workflows are owning controls, because they do not attempt to seize input or send new UI events.

## Operator report template (Agent Mode workflow performance)

Use this as a standard “handoff” message when an Agent Mode automation run is `DEFERRED` or when you need to summarize workflow quality without executing UI automation.

**Report (fill in):**

- Run ID: `<run_id>`
- Outcome: `status=<PASS|DEFERRED|FAIL>`, `pass=<true|false>`
- Deferral: `deferred_count=<N>` (and `defer_reason=<...>` if present)
- Controls state: `paused=<true|false>`, `owner=<...>`
- Deferred queue: `entries=<N>` (optionally `unique_action_ids=<M>` if you computed it)
- Notes: recent errors/warnings, suspected flake source (focus/timing/OCR), and whether verify phase was enabled

**Safe commands to gather the fields (read-only):**

- Latest workflow summary (prints run id, status, pass, paused, deferred_count):
  - `Scripts/python.exe -c "import glob,os,json; p=max(glob.glob('logs/tests/workflow_summary_*.json'), key=os.path.getmtime); obj=json.load(open(p,'r',encoding='utf-8')); ctx=(obj.get('workflow_info') or {}).get('interaction_context') or {}; paused=(json.load(open('config/controls_state.json')) if os.path.exists('config/controls_state.json') else {}).get('paused'); print('run_id', os.path.basename(p).replace('workflow_summary_','').replace('.json','')); print('status', obj.get('status')); print('pass', obj.get('pass')); print('paused', paused); print('deferred_count', ctx.get('deferred_count')); print('defer_reason', ctx.get('defer_reason'))"

- Controls snapshot (stale/owner/paused):
  - `Scripts/python.exe Scripts/controls_inspect.py --stale-seconds 300`
  - Note: live deferred execution fails closed if controls state is stale; refresh it before running `--live`:
    - `Scripts/python.exe Scripts/controls_set_paused.py --paused false`

- Deferred queue size (counts JSONL lines):
  - `Scripts/python.exe -c "import os; p='logs/actions/deferred_workflow_actions.jsonl'; n=sum(1 for _ in open(p,'r',encoding='utf-8',errors='ignore')) if os.path.exists(p) else 0; print('queue_entries', n)"

- Deferred queue inspection (safe; no execution):
  - `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --list --max 20`
  - `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --dry-run --max 5`
  - Prefer scoping to a workflow run id (recommended for repeatable actions):
    - `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --list --run-id <run_id> --max 20`
    - `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --dry-run --run-id <run_id> --max 5`
  - Target a single deferred action id:
    - `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --dry-run --id <action_id> --max 1`
    - (recommended) scope to a run id:
      - `Scripts/python.exe Scripts/run_deferred_workflow_actions.py --dry-run --run-id <run_id> --id <action_id> --max 1`

Tip: if you want a single place to read these without manual commands, regenerate the context pack and read its “Runtime state (safety)” + deferred queue lines:

- `Scripts/python.exe Scripts/prepare_context_pack.py`
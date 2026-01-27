# Agent Health Checklist

This checklist covers quick, non-disruptive checks you can run before or after workflows to confirm that Agent Mode, OCR, and commit/verify loops look healthy. All of these commands are read-only with respect to UI input (no new clicks/keys).

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
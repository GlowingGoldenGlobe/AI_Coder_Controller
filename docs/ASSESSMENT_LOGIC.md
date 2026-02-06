# Assessment Logic Guide (Agent Mode)

This guide defines the logic the Agent Mode assessment role must follow when evaluating assessment schedule validity and other assessment outputs.

## Core principles

1) **Do not interfere with safe, ongoing workflow activity** unless an assessment indicates a *blocking* safety or integrity issue.
2) **Record upgrade tasks** whenever assessment detects missing or inconsistent schedule configuration.
3) **Prefer immediate fixes** for non-blocking issues, but **defer** when safety or workflow integrity could be impacted.

## Decision algorithm (schedule assessment)

### Inputs

- `schedule_ok`: boolean (from `assessment_schedule_<run_id>.md`)
- `errors[]`: list of schedule errors
- `warnings[]`: list of schedule warnings
- `workflow_status`: PASS / DEFERRED / FAIL
- `agent_mode_active`: boolean

### Output

- `decision.action`: one of `no_action`, `record_tasks_only`, `schedule_upgrade`, `apply_immediate_upgrade`
- `decision.reason`: short rationale
- `upgrade_tasks[]`: list of tasks to fix

### Algorithm (prefer immediate)

1) If `schedule_ok` and no errors/warnings:
   - `decision.action = no_action`
   - `decision.reason = "schedule valid"`

2) If there are **warnings only**:
   - `decision.action = apply_immediate_upgrade`
   - `upgrade_tasks = warnings` (summarized)
   - `decision.reason = "non-blocking schedule warnings; prefer immediate"`

3) If there are **errors**:
   - Classify severity:
     - **Blocking** if schedule file is missing or JSON is invalid.
     - **Non-blocking** if individual entries are malformed but overall schedule file is readable.
   - If blocking:
     - `decision.action = schedule_upgrade`
     - `decision.reason = "schedule missing/invalid; avoid disrupting current run"`
   - If non-blocking:
       - `decision.action = apply_immediate_upgrade`
       - `decision.reason = "schedule entries invalid but current workflow can continue; prefer immediate"`

4) If workflow is already FAIL due to unrelated issues:
   - **Defer** immediate upgrades.
   - Set `decision.action = schedule_upgrade` and record tasks only.

### Immediate upgrade conditions (default, with deferrals)

Immediate upgrades are **preferred**, but must be deferred if any of the following are true:
- The schedule file is missing or JSON is invalid (blocking).
- The workflow is currently FAIL due to unrelated issues.
- Live UI automation is in progress (avoid interfering with controls).

When none of the above apply, set `decision.action = apply_immediate_upgrade`.
- `agent_mode_active` is true (assessment role is running under Agent Mode)
- The upgrade is safe and non-interactive (file-only change)
- No live UI automation is in progress
- A dedicated operator/automation lane has approved immediate changes

## Required behavior when errors occur

- Always write `upgrade_tasks[]` to the workflow summary under `workflow_info.assessment_schedule_upgrade_tasks`.
- Always include the decision summary in `assessment_schedule_<run_id>.md`.
- Always post a `assessment_schedule_checked` event to Chat Lanes (triage lane) with `ok` and task count.

## Examples

### Example A: Missing schedule file
- errors: ["assessment_schedule.json missing"]
- decision: `schedule_upgrade`
- tasks: ["Create config/assessment_schedule.json with required fields"]

### Example B: Missing `interval_seconds` for an interval entry
- errors: ["assessment 'chat_lanes_assessment' missing interval_seconds"]
- decision: `record_tasks_only`
- tasks: ["Add interval_seconds to chat_lanes_assessment"]

### Example C: Warnings only (unknown cadence)
- warnings: ["assessment 'x' uses unknown cadence 'weekly'"]
- decision: `record_tasks_only`
- tasks: ["Normalize cadence to per_run/manual/interval"]

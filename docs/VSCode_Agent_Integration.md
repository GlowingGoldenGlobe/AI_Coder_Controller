# VSCode Agent Integration

This document summarizes how AI_Coder_Controller integrates with VS Code and GitHub Copilot, adapted from the provided VSCode Agent Integration Instructions and aligned with current implementation.

## Purpose
- Open and control VS Code windows.
- Navigate files, terminals, and Copilot Chat.
- Bridge ambiguous objectives to Copilot (PHI‑4 reasoning via Copilot Chat).
- Keep structured logs of actions and messages.

## Core Capabilities (Implemented)
- Open VS Code: `VSBridge.open_vscode()`; focuses an existing window when found.
- Open folder: `Ctrl+K`, `Ctrl+O` with path — `VSBridge.open_folder(path)`.
- Open files by name: `Ctrl+P` — `VSBridge.open_file_quick(pathOrName)`.
- Open/Focus terminal: `Ctrl+\`` with palette fallbacks — `VSBridge.focus_terminal()`.
- Open/Focus Copilot Chat: Command Palette → “Open View: GitHub Copilot Chat” — `VSBridge.focus_copilot_chat_view()`.
- Compose messages to Copilot: `VSBridge.ask_copilot(text)` or `compose_message_vscode_chat(text)`.
- Scroll and read Copilot responses: `VSBridge.scroll_chat(direction, steps)` plus image analysis via `ImageAnalyzer`/`CopilotOCR` and `VSBridge.read_copilot_chat_text()`.
- Multiple instances: focuses most likely window via `WindowsManager` title match; can open new windows via palette if needed.

## Workflow
1. Objectives parsed by `Policy` from `config/objectives*.md`.
2. Clear tasks execute locally; ambiguous tasks route to Copilot.
3. Copilot flow: focus chat → send message → optional scroll → image capture + analysis (templates/elements, optional OCR text) → append to `projects/Self-Improve/improvements.md` (de‑duplicated).
4. Edits and terminal runs are driven by hotkeys and typed commands.

## Terminal + venv Rule (Agent Requirement)
- For any terminal commands that operate on venv projects, Agent AIs must first run `Scripts/activate` in that terminal.
- Keep using the same terminal session after activation to avoid losing the venv context.
- If a terminal is not activated, prefer `Scripts/python.exe` for Python commands to ensure the correct interpreter is used.

## Safety & Guardrails
- ESC emergency pause; UI “Pause Controls”.
- Intermittent control cycle (default 120s active / 5s release).
- PyAutoGUI failsafe (top-left corner abort).
- Input rate limits for clicks/keys.

## Shared Control Ownership
- State file: config/controls_state.json records who "owns" automation controls.
- Keys:
	- owner: current owner label (e.g. "agent", "workflow_test", "copilot_app_test", or empty when free).
	- in_use: whether the controller is currently inside its active control window.
	- in_control_window / control_remaining_s: timing details for the active window.
	- ts: last update timestamp (Unix seconds since epoch).
- Helper functions in src/control_state.py:
	- get_controls_state(root): read and parse controls_state.json (best effort).
	- set_controls_owner(root, owner): set or clear the logical owner and update ts.
	- update_control_window(root, in_control, remaining_s): update the control window status and ts.
	- is_state_stale(state, max_age_s): pure helper to check whether a previously-read snapshot is older than a caller-supplied age threshold; it never mutates the snapshot.
- The main controller sets owner="agent" while Agent Mode is running and clears it on shutdown.
- Selected scripts claim a temporary owner (for example "workflow_test" or "copilot_app_test") and restore the previous owner on exit.
- All Controller-based tools install a window_gate that reads controls_state.json and only send input when the owner rules permit (typically when owner is empty or matches their workflow).

For quick inspection, you can run the helper script Scripts/controls_inspect.py, for example:

- python Scripts/controls_inspect.py --stale-seconds 300

This prints the current controls_state.json contents, reports whether the snapshot looks older than the threshold you provided, and shows the currently recorded owner. It does not change ownership or modify the state file; it is safe to run while other workflows are active.

If a workflow run is interrupted and leaves ownership stuck on `workflow_test`, you can clear it safely with:

- python Scripts/controls_release_owner.py --if-owner workflow_test

This only clears ownership when the current owner matches `workflow_test` (unless you pass `--force`). It does not change the paused state.

## Self‑Improve Integration
- Metadata generation: `src/self_improve.py::write_metadata_file()`.
- Copilot handoff: sends metadata or summaries to chat.
- Image analysis + optional OCR appends Copilot responses to `projects/Self-Improve/improvements.md`.

## Logging
- Human-readable: `logs/run.log` and `logs/self_improve.log`.
- Structured JSONL: `logs/actions/actions.jsonl` via `src/jsonlog.py`.
- Image analysis debug images: `logs/ocr/`.

## Configuration
- config/policy_rules.json: hotkeys, bounds, vsbridge.delay_ms, vsbridge.dry_run, and measurement thresholds/retry settings.
- config/ocr.json: monitor/ROI, tesseract_cmd, tesseract_psm, chat_settle_ms.
- config/templates.json: optional curated UI templates (for example chat_input.templates pointing at assets/ui_templates/curated images).
- config/vscode_orchestrator.json: multi-window chat orchestrator settings, message_strategy (first or cycle), and auto-message templates.

For measurement-only validation (no UI clicks), you can run a small smoke test that captures a single image-analysis frame and reports which templates match:

- python Scripts/measurement_smoke_test.py

This writes a JSON summary under logs/tests/measurement_smoke_*.json with the captured image path, element count, configured threshold, and per-template match flags.

For an offline view of commit/verify health (no new UI activity), you can summarize recent commit+verify logs with:

- python Scripts/commit_verify_summary.py --log logs/actions/commit_verify_2plus2.log --log logs/actions/commit_verify_stability.log --hours 12

This writes a JSON summary under logs/tests/commit_verify_summary_*.json with basic per-log and overall pass/fail counts and success rates, filtered to roughly the last N hours when you use --hours.

## PHI‑4 Notes
- Current `src/phi4_planner.py` is a local stub (no external runtime).
- Copilot (cloud) provides reasoning/synthesis in chat; no extra installs required beyond `requirements.txt` and Tesseract for OCR.

## Tips
- If OCR misses content, raise `chat_settle_ms` and adjust `region_percent` in `config/ocr.json`.
- Increase `vsbridge.delay_ms` if automation feels racy.
- Use the UI Automation toggle to switch `dry_run` on/off quickly.
 - Check `config/controls_state.json` when a workflow reports "Controls owned by another workflow" to see which owner is active.

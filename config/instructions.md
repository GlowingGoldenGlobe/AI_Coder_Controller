# Instructions
- Software to use:
  - VSCode for code editing and terminal
  - Copilot Chat inside VSCode for clarifications
- Execution flow:
  1. Parse objectives from this folder (objectives.md, any txt/md uploaded via UI)
  2. Deterministic actions: move, click, type, open views
  3. Ambiguous actions: open Copilot Chat, compose questions, read responses, summarize and log
  4. Keep logs of run events, pauses, stops, and user messages
- Safety:
  - PyAutoGUI fail-safe enabled (top-left abort)
  - Rate limits on clicks/keys per minute
  - Emergency stop via ESC

## Agent Mode Instructions (Measurement-Based Orchestration)

Objective:
Enable Agent Mode in VS Code to analyze monitor screen pictures, perform click and sendkeys/Enter events, and copy information from other software such as Copilot messages.

Instructions:

1. Screen Capture and Measurement
- Use src/capture.py (or equivalent helpers) to take screenshots of the foreground window or monitor.
- Use image-based measurement utilities (for example Scripts/measurement_smoke_test.py plus config/templates.json) to measure regions in the screenshot (bounding boxes, pixel similarity).
- Compare screenshots against assets/ui_templates and assets/ui_templates/curated for known UI elements (chat input, buttons).

2. UI Automation
- Use src/control.py and src/windows.py to move the mouse and send keyboard input.
- Implement click(x,y) using measured coordinates from capture.
- Implement send_keys(text) and press_enter() to commit actions in the focused input region.
- Use src/control_state.py to verify readiness before acting; abort if evidence is weak.

3. Copilot Integration
- Use src/messaging.py (and VSBridge helpers) to interact with Copilot chat regions.
- Prefer image/measurement-based readiness checks; treat OCR text as legacy/debug only.
- When copying content, store captured image snippets or message metadata in projects/Self-Improve/improvements.md and logs/actions/actions.jsonl.

4. Workflow Loop
- Modify/extend src/main.py with a measurement-based loop phase:
  capture → measure → act → copy → log
- Each tick:
  - Capture screenshot
  - Compare to templates
  - If match, perform an atomic commit (click/keys) at the measured coordinates
  - Log evidence (coordinates, similarity score, image hash) for later assessment.

5. Configuration
- Extend config/policy_rules.json with:
  - measurement.threshold
  - measurement.retry_attempts
  - measurement.backoff_ms
- Use config/templates.json to define known UI elements and reference images (already used by Scripts/measurement_smoke_test.py).

6. Testing
- Use Scripts/measurement_smoke_test.py to validate template matching without sending input.
- Use Scripts/navigation_test.py, Scripts/ocr_commit_test.py, and the Test/Gather/Assess workflow to validate that measurement-based readiness checks and actions behave as expected.

7. Logging and Evidence
- Store all measurement logs in logs/actions/actions.jsonl (with clear event names).
- Archive evidence images in Archive_OCR_Images_Assessments/ when a measurement-driven action fails or is ambiguous.
- Maintain structured JSONL logs for every tick so measurement-based behavior can be audited and improved.

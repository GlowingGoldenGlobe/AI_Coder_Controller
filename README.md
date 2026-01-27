# AI_Coder_Controller

Windows-first automation controller with a safe, observable workflow loop:

- Initial composition: 2025-12-14 (based on earliest file mtimes in the repository)
- Records the screen (segmented rolling capture)
- Safely automates keyboard/mouse with intermittent control windows and ESC pause
- Centralizes all terminal actions via an Integrated Terminal Agent
- Interacts with VS Code Chat and the Windows Copilot app
- OCRs Copilot responses and commits them to project notes
- Defers/queues prompts while busy; can auto-run a timed external commit loop after Stop

## Quick Start
- Activate venv (PowerShell):
  - Scripts/Activate.ps1
- Install deps:
  - pip install -r requirements.txt
- Run:
  - python -m src.main
- Headless Agent Mode (no UI, executes objectives):
  - python -m src.main --headless --agent --duration 60
  - python -m src.main --headless --agent --objectives config/objectives.md --duration 60
- Desktop Shortcut:
  - scripts/create_desktop_shortcut.ps1

Recommended tools to validate the setup:
- Navigation test: `python scripts/navigation_test.py`
- OCR smoke test: `python scripts/ocr_smoke_test.py`
- OCR commit test (captures and appends to notes): `python scripts/ocr_commit_test.py`

## Structure
- config/: policy, objectives, instructions, ocr.json
- projects/Self-Improve/: objectives, instructions, improvements.md
- src/: capture/control/policy/ui/vsbridge/windows/ocr/main
- vscode_automation/: VS Code multi-window orchestrator
- logs/: run.log, self_improve.log, ocr/
- logs/actions/: actions.jsonl (structured JSONL action log)
- recordings/: mp4 files (auto)
- scripts/: create_desktop_shortcut.ps1, html_to_image.py, compose_image.py, copilot_commit.ps1, copilot_commit_start.ps1, copilot_commit_stop.ps1, navigation_test.py, assess_windows.py, close_idle_powershell.py, ocr_commit_test.py
  - Scripts/vscode_multi_keepalive_smoke.py: multi-window chat keepalive smoke test
  - Scripts/vscode_multi_keepalive_daemon.py: long-running multi-window chat keepalive
  - Scripts/orchestrator_agent.py: headless Orchestrator Agent Mode entrypoint
- docs/: SETUP_OCR.md, VSCode_Agent_Integration.md, ERROR_COMMANDS.md
  - PHI4_Commit_Tuning.md
  - COPILOT_CONTEXT_PACK.md
  - AGENT_HEALTH_CHECKLIST.md (quick, non-disruptive checks for controls/OCR/commit-verify health)

## VS Code Multi-Window Orchestrator

The controller includes an image-driven **orchestrator** that keeps Copilot/VS Code chat workflows moving across multiple VS Code windows. In normal use, **Agent Mode itself is the orchestrator**: when Agent Mode is running, it owns automation controls and continuously calls the orchestrator as part of its main loop until you explicitly STOP.

- Discovery: `vscode_automation/window_set.py` scans top-level windows via Win32 and identifies all visible VS Code instances (Code.exe / "Visual Studio Code").
- Chat ROI analysis: `vscode_automation/chat_buttons.py` uses the existing `CopilotOCR` configuration (config/ocr.json) to capture the VS Code chat region, analyze the image for button-like UI elements/templates, and choose a safe primary button to click. Decisions are made from the captured image, not OCR text.
- Multi-window keepalive: `vscode_automation/multi_window_keepalive.py` composes these pieces into a **multi-window chat keepalive orchestrator** that:
  - Iterates over every VS Code window.
  - Focuses each window in turn.
  - Observes the chat region via image capture and UI-element detection.
  - When the image analysis finds actionable button-like elements, moves the mouse over the selected button and clicks it.
- Logging: every observation/click attempt is written to structured JSONL logs under `logs/actions/` for later assessment and self-improvement.
 - Integration: `src/main.py` wires an optional `MultiWindowChatKeepalive` into both the headless and UI tick loops. On a configurable interval (`interval_s` in `config/vscode_orchestrator.json`), it calls `cycle_once()`, logs a summary event, and swallows any internal errors so the main loop keeps running.

### Agent-as-Orchestrator (Strict Integration)

- When Agent Mode is active, it is the **sole owner** of mouse/keyboard automation as recorded in `config/controls_state.json` (`owner="agent"`).
- In this state, the in-process orchestrator is driven from inside Agent Mode's own loop; it does not run as a separate, competing workflow.
- External helpers such as `Scripts/vscode_multi_keepalive_smoke.py` and `Scripts/vscode_multi_keepalive_daemon.py` respect the same shared state and will not send input while `owner` is set to another workflow (for example `"agent"` or `"workflow_test"`).
- A dedicated Orchestrator Agent entrypoint, `Scripts/orchestrator_agent.py`, simply launches `python -m src.main --headless --agent` with `config/objectives_orchestrator.md`, treating **this Agent instance** as the orchestrator that keeps VS Code Agent Mode chats and tabs active.

This orchestrator prevents long-running workflows from stalling when *any* VS Code window requires a chat button action (e.g., "Continue generating") by safely and repeatedly nudging each window's chat UI.

### Orchestrator Message Options

Message composition and sending are controlled via JSON, not hardcoded logic:

- Config file: `config/vscode_orchestrator.json`
- Fields:
  - `action_hints`: words/phrases that suggest a button click is needed (e.g., "keep", "allow").
  - `message.compose_when_prompts_match`: phrases in the chat text that mean "ask for instructions".
  - `message.default_templates`: ordered list of templates such as "Continue.", "What's next?", and a request to summarize objectives/read the workflow README when tasks are done.
  - `message.allow_auto_send`: when true, the orchestrator can focus the chat input, type a selected template, and send it using `message.send_keys` (default `Ctrl+Enter`).
  - `message_strategy`: how to pick templates (`"first"` or `"cycle"`).

The orchestrator itself stays task-agnostic: it does not inspect objectives or README directly. Instead, you encode those behaviors into message templates (for example, asking the chat agent to report objective state or to read the workflow README when no tasks remain).

### Relationship to Agent Mode

- Conceptually, Agent Mode is the **orchestrating agent**; the multi-window orchestrator is the mechanism it uses to keep chats from stalling.
  - Agent Mode (see "VS Code Agent Workflow Loop" below) runs objectives, assesses UI state, and decides what actions to take.
  - The orchestrator focuses only on **chat keepalive via image-based UI detection + mouse/keyboard actions** across all VS Code windows and is invoked from inside Agent Mode's loop when enabled.
- The main runner (`src/main.py`) treats the orchestrator as an optional, continuous background loop:
  - If `vscode_automation` imports and initializes successfully, the tick functions periodically call `MultiWindowChatKeepalive.cycle_once()`.
  - If initialization or a cycle raises, the error is logged to `logs/actions/actions.jsonl` under an `orchestrator` event and the controller keeps running.
- Standalone orchestrator cycles (smoke/daemon) are primarily for diagnostics and must respect `config/controls_state.json`; they yield whenever Agent Mode or another workflow currently owns controls.

Helpers & daemon:

- One-shot helper (for other modules/agents):
  - `from vscode_automation import run_multi_window_keepalive_cycle`
  - `summary = run_multi_window_keepalive_cycle()`  # single orchestrator tick
- Standalone daemon process (separate workflow, can be launched by another agent):
  - `Scripts/python.exe Scripts/vscode_multi_keepalive_daemon.py --interval-s 6`

Quick smoke test:

- With the venv active and VS Code windows open, run:
  - `Scripts/python.exe Scripts/vscode_multi_keepalive_smoke.py`
- The script will:
  - Construct `Controller`, `WindowsManager`, and `CopilotOCR` from your existing configuration.
  - Run a single multi-window keepalive cycle.
  - Print a JSON summary of windows scanned and actions taken.

## Controls & Features
- Run/Pause/Resume/Stop: orchestrate execution
- Focus VS Code / Focus Terminal: reliable window focusing
- Pause Controls (button) + ESC: pause/resume AI mouse+keyboard immediately
- Color timer: near Pause Controls shows Active (green), Release (orange), Paused (red)
- 2-min/5-sec cycle: AI controls for 120s, user-exclusive 5s, repeats
- OCR Copilot: captures Copilot Chat or Windows Copilot app and appends to projects/Self-Improve/improvements.md (de-duplicated)
- OCR Observer: optional “movie” stream of OCR frames (configurable interval)
- Segmented screen recording: rolling segments with cleanup rules (age + max_keep)
- Cleanup scheduler: deletes old debug frames/segments per rules
  - Default rules clean `logs/ocr` images and `recordings/segments` videos; now also `logs/screens` PNG/MP4 (requires `.assessed` marker; 5s retention, keep latest 50)
  - The live recorder and the Commit+Record wrapper auto-create `.assessed` markers for their outputs to enable safe, fast cleanup.
  - Standalone test scripts may also run a one-shot cleanup pass on exit (set `AI_CONTROLLER_RUN_CLEANUP=0` to disable).

### Agent Mode & Terminal Agent
- Single gateway for terminal actions; all shell runs go through the VS Code integrated terminal.
- Post-stop terminal commit: pending typed commands get Enter on Stop.
- Agent Mode can auto-run on startup; toggleable in the UI.

**Note – recorded correction:** An internal reporting error was made on 2025-12-18 where the agent's earlier notes implied pixel-level image inspection. That statement was incorrect; the assessment generator used OCR text fields. See the recorded report: [Archive_OCR_Images_Assessments/AGENT_ERROR_REPORT_20251218.md](Archive_OCR_Images_Assessments/AGENT_ERROR_REPORT_20251218.md) for details and remediation steps.

### Copilot Messaging: Quiet & Deferred
- Quiet-send: defers Copilot prompts while busy or not idle; flushes when idle or on Stop.
- “After Stop” queue: prompts can be scheduled to send when Stop is pressed.
- Prefer the Windows Copilot app or VS Code Chat (configurable).

### External Commit Loop (PowerShell)
- Standalone loop to focus Copilot, type a message (optional), and commit (Ctrl+Enter then Enter) on a timer.
- Launched externally so it runs independent of VS Code’s terminal.
- Dedupe: launcher skips creating a duplicate infinite loop if one is already running.
- Logging: writes LAUNCH/LAUNCHED/START/COMMIT and FOCUS entries to logs/actions/copilot_commit.log.

## VS Code Agent Workflow Loop
- Trigger: Enable Agent Mode (UI) or run headless Agent Mode with `--headless --agent` and objectives in `config/objectives.md`.

- High-level goal: run a safe, observable loop that repeatedly assesses UI state, performs small, verifiable actions, observes results (OCR/UIA), and adapts or aborts on ambiguous evidence.

- Phases (clearer)
  - Assess: inventory foreground windows, verify the target app (e.g., VS Code) is focused and that required UI elements (chat input, file tree, terminal) are present.
  - Recover: if assessment fails, run deterministic recovery steps (close overlays, open conversation, focus input, reopen file). Fail closed when evidence is weak.
  - Gate & Act: lock inputs to the targeted window, apply the smallest atomic commit (single paste/Enter/click) required by the objective.
  - Observe & Verify: immediately OCR/UIA-observe the result of every commit action and log the evidence. If the result doesn't match expectations, revert or retry according to policy.
  - Stop & Flush: on Stop, flush deferred Copilot messages, commit any pending terminal input, and optionally launch configured post-stop commit loops.

- Pre-action checklist (run every tick before any commit)
  - Is target window foreground and allowed? (if not, abort tick)
  - Is the chat/terminal/file input focused and empty/ready? (OCR/UIA check)
  - Are there modal dialogs/overlays that would intercept input? (detect and close or abort)
  - Is the controller in an Active window period (not Release or Paused)?

- Recovery actions (ordered, deterministic)
  1. Send `Esc` to clear overlays and re-assess.
  2. Focus the expected control (Ctrl+L/Ctrl+`/Ctrl+P, or click center of known ROI) and re-verify with OCR/UIA.
  3. If conversation/file not present, open the most recent conversation or file list and try again.
  4. If still ambiguous after N attempts (configurable), fail closed for that objective and log an assessment result file to `Archive_OCR_Images_Assessments/`.

- Observability & metrics (log each tick)
  - `assessment_result` (pass/fail + evidence image path)
  - `action` (what was attempted) and `action_outcome` (success/error)
  - OCR text delta and hashes (for de-dup)
  - Retry counts and time spent per objective

- Config knobs to tune behavior (examples for `config/policy_rules.json`)
  - `agent.retry_attempts`: 2
  - `agent.retry_backoff_ms`: 500
  - `agent.max_nav_steps`: 5
  - `ocr.chat_settle_ms` / `ocr.app_settle_ms` for longer waits when UI is slow
  - `copilot.defer_when_busy` and `copilot.quiet_idle_ms`

- Pseudocode (refined)

```text
loop tick:
  assessment = assess_foreground()
  log(assessment)
  if not assessment.ready:
    recovered = try_recover(assessment)
    if not recovered:
      log_fail_and_archive_evidence(assessment)
      continue

  gate_inputs(assessment.target)
  outcome = perform_atomic_commit(objective_step)
  evidence = observe_after_commit()
  if not evidence.matches_expected:
    if retries_left:
      retry_with_backoff()
    else:
      log_fail_and_archive_evidence(evidence)
  else:
    mark_step_complete()
```

- Failure handling & learning
  - Produce an assessment markdown in `Archive_OCR_Images_Assessments/` linking OCR images and a short note of next safe action.
  - Use these artifacts to iteratively refine recovery rules rather than adding heuristic position-based clicks.

- Small actionable changes I applied
  - Clarified the loop phases and added a short pre-action checklist.
  - Added deterministic recovery steps and an explicit fail-closed policy.
  - Documented observability outputs and example config knobs you can tune.

If you'd like, I can also:
- Add a short `scripts/assess_and_archive.py` helper that writes the assessment markdown automatically on failure.
- Insert a small `agent` section in `config/policy_rules.json` with the example knobs above.

### Foreground App Assessment & Navigation (pseudocode)

Many automation failures are not "typing failed" — they are "wrong UI state" (no conversation selected, overlay open, focus in the wrong field, etc.). The recommended pattern is:

1) **Assess** what app is foreground and what UI state it is in.
2) **Recover** into a known-good state for the action you want (e.g., "chat input is focused").
3) **Act** (type/click) only when the assessment says it is safe.

General loop (app-agnostic):

```text
function act_in_foreground_app(goal):
  fg = get_foreground_window_info()
  if fg is disallowed (VS Code when targeting app, terminals, etc.):
    return FAIL_CLOSED

  assessment = assess_ui_state(fg)
  log(assessment)

  if assessment.needs_recovery:
    ok = recover_to_ready_state(assessment)
    if not ok:
      return FAIL_CLOSED

  gate_inputs_to(fg)
  return perform_goal_action(goal)
```

Copilot app example (send message):

```text
function send_copilot_message(text, optional_attachment_path):
  focus_copilot_app()
  assert copilot is foreground AND VS Code is NOT foreground

  a = assess_copilot_window():
      - focused control type/name (UIA)
      - is chat input focused? (UIA or OCR)
      - does sidebar have conversation list items?
      - does it look like "no conversation selected"?

  if a.needs_conversation_open:
      open_most_recent_conversation_from_sidebar()  # UIA click/invoke

  close_overlays_with_esc()
  click_bottom_center_to_focus_input()

  if optional_attachment_path:
      open_attach_ui_and_attach_file(optional_attachment_path)

  type_or_paste(text)
  press_enter_to_send()
```

Browser example (navigate / search safely):

```text
function browser_navigate_or_search(url_or_query):
  focus_browser_window()
  assert browser is foreground

  a = assess_browser_window():
      - is an address bar focused? (UIA or OCR hints)
      - is a modal/permission prompt visible?
      - is a download dialog stealing focus?

  if a.modal_visible:
      dismiss_or_fail_closed_based_on_policy()

  focus_address_bar()  # Ctrl+L usually
  type(url_or_query)
  press_enter()
```

Key principle: **don’t “just send keys”**. Always do a quick assessment (UIA/OCR + window identity) so the agent can choose the right navigation recovery step.

#### Observation Rules (learned vs non-learned)

This controller distinguishes **navigation** actions from **commit** actions:

- Navigation actions: `Tab`, arrow keys, moving the mouse (cursor reposition), scrolling.
- Commit actions: `Enter`, mouse click, typing/pasting text.

Pseudocode rule:

```text
if sequence is NOT learned:
  observe OCR/UIA after each navigation step
else (sequence is learned AND last run succeeded):
  do not OCR after every navigation step

always:
  OCR/UIA-observe immediately before any commit action (click / Enter / text input)
```

#### Copilot App: Attach a File (no blind picks)

```text
function attach_file_in_copilot_app(path):
  focus_copilot_app()
  assert copilot is foreground

  click '+' / 'More options'
  observe (OCR) that flyout menu is visible

  options = OCR_read_flyout_labels_and_images()
  candidates = enumerate_clickable_items_in_flyout()

  # Score candidates by OCR evidence; do not pick by position.
  pick = argmax(candidates, score_by_ocr(['upload','add files','file','attach','browse','select']))
  if pick.score < threshold:
    FAIL_CLOSED  # no guessing

  observe before click(pick)
  click(pick)

  wait_for_file_dialog()
  click File name input (UIA bbox center)
  observe before paste
  paste full path
  press Enter
```

Debugging commands for this workflow are in [docs/ERROR_COMMANDS.md](docs/ERROR_COMMANDS.md).

#### Assessment Description Result Files

When a workflow fails (or when you are validating a newly-learned procedure), produce an **assessment description result file** that links the OCR evidence images and records what was observed + what action should come next. These files make it easy to reuse the same “assess and learn” method for other workflows.

- Archive location: [Archive_OCR_Images_Assessments](Archive_OCR_Images_Assessments)
- Generator: [Scripts/generate_ocr_observations_md.py](Scripts/generate_ocr_observations_md.py)
- Example output: [Archive_OCR_Images_Assessments/OBSERVED_OCR_IMAGES_20251218.md](Archive_OCR_Images_Assessments/OBSERVED_OCR_IMAGES_20251218.md)

Command example:

```powershell
C:/Users/yerbr/AI_Coder_Controller/Scripts/python.exe Scripts/generate_ocr_observations_md.py --out Archive_OCR_Images_Assessments/OBSERVED_OCR_IMAGES.md --file-substr ModulesList_fs_agent_20251218_013825.txt
```

Reusable pattern:

1) Run the workflow (or a targeted test script)
2) Generate the OCR assessment report for that run (by file substring or by “latest window”)
3) Review each linked image section and decide whether the next action is justified by the observed state
4) Patch the recovery/targeting logic (fail closed if evidence is weak)
5) Re-run and archive the new report

## AI API Agent Workflow (Phi‑4)
- Purpose: Optional planner/agent connectivity for experiments and health checks.
- Enable: Configure `phi4` in `config/policy_rules.json` (endpoint, api_key, model, timeout). Disabled by default.
- Health Check: Use the UI “Planner connectivity test” (calls `phi_client.ping()`), logs to `logs/actions/actions.jsonl`.
- Usage: When enabled, the runtime initializes a Phi‑4 client; messages and metadata flows can leverage the planner path. See `src/main.py` and `src/phi4_client.py` for integration points.
- Notes: Keep timeouts modest; handle failures gracefully—controller continues without planner if unavailable.

## VS Code & Copilot Automation
- Configure in config/policy_rules.json:
  - vsbridge: { "dry_run": false, "delay_ms": 350 }
  - copilot: prefer_app, defer_when_busy, quiet_idle_ms, auto_commit_after_stop, timing, message, title, log path
- Objectives examples (config/objectives.md):
  - Open VSCode
  - Open folder: C:\\Users\\...\\AI_Coder_Controller\\
  - Open file: src\\ui.py
  - Focus VSCode / Focus Terminal
  - Scroll chat down x 3 (UI also has a steps control)
  - Terminal: git status
  - Ask Copilot to clarify: "..."
  - Insert summary into src\\instructions.md
  - Fallback behavior: When `copilot.prefer_app` is true but the Copilot app cannot be focused, the controller automatically falls back to VS Code Chat for sending the message.

## Commit Tuning (PHI‑4)
- See docs/PHI4_Commit_Tuning.md for Conventional Commits, safe branching, and a Copilot prompt to prepare messages.

## OCR Setup
- See docs/SETUP_OCR.md
- Ensure Tesseract is installed and config/ocr.json points to it (or it’s on PATH)
- Test: python scripts/ocr_smoke_test.py
- Commit test: python scripts/ocr_commit_test.py (captures App + Chat and appends to improvements.md; writes a JSON report)

## Logs
- Text log: logs/run.log (general); logs/self_improve.log (Self‑Improve)
- JSONL log: logs/actions/actions.jsonl (one JSON object per line)
- Commit loop: logs/actions/copilot_commit.log (PowerShell loop LAUNCH/START/COMMIT)
- Test reports: logs/tests/*.json (+ *.md where applicable)

## Utilities & Health
- Window assessment: `python scripts/assess_windows.py` (inventory, foreground, VS Code/Copilot presence, duplicate loops, OCR idle score for PowerShell)
- Close idle PowerShell: `python scripts/close_idle_powershell.py` (OCR-based selection; closes exactly one idle window)
- Start commit loop (external window):
  - Infinite loop with dedupe:
    ```powershell
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\copilot_commit_start.ps1 -Mode app -StartAfterSeconds 5 -RepeatSeconds 10 -RepeatCount 0 -Message "Auto message — see projects/Self-Improve/next_steps.md" -Title "Copilot" -LogPath "logs/actions/copilot_commit.log"
    ```
  - Stop loops:
    ```powershell
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\copilot_commit_stop.ps1
    ```

## Screen Recording & Live Preview

- One-off screenshot / time-lapse frames: `scripts/capture_screen.ps1`
  - Single shot with timestamp:
    ```powershell
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\capture_screen.ps1 -Out "logs\screens\screen_$(Get-Date -Format yyyyMMdd_HHmmss).png" -StampTime
    ```
  - Time-lapse frames (10s @ 2 fps):
    ```powershell
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\capture_screen.ps1 -OutDir "logs\screens\rec_$(Get-Date -Format yyyyMMdd_HHmmss)" -Seconds 10 -Fps 2 -StampTime
    ```
  - Optional MP4/GIF render (requires ffmpeg on PATH):
    ```powershell
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\capture_screen.ps1 -OutDir logs\screens\rec -Seconds 5 -Fps 2 -OutVideo logs\screens\out.mp4
    ```

- Live recorder (Python): `scripts/monitor_live.py`
  - Auto backend (prefers dxcam, falls back to mss):
    ```powershell
    C:/Users/yerbr/AI_Coder_Controller/Scripts/python.exe scripts/monitor_live.py --seconds 5 --fps 12 --out logs/screens/live_auto.mp4
    ```
  - Force mss (stable on any GPU):
    ```powershell
    C:/Users/yerbr/AI_Coder_Controller/Scripts/python.exe scripts/monitor_live.py --seconds 5 --fps 12 --out logs/screens/live_mss.mp4 --backend mss
    ```
  - Preview window (ESC to stop):
    ```powershell
    C:/Users/yerbr/AI_Coder_Controller/Scripts/python.exe scripts/monitor_live.py --preview --fps 15 --backend mss
    ```

- Commit + record wrapper: `scripts/copilot_commit_with_record.ps1`
  - Short, bounded run with synchronized recording:
    ```powershell
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\copilot_commit_with_record.ps1 -Mode app -StartAfterSeconds 1 -RepeatSeconds 4 -RepeatCount 1 -Message "Record sync test" -LogPath "logs/actions/commit_with_record.log" -Record -RecordFps 10 -RecordBackend auto -Wait
    ```
  - Note: If `-RepeatCount 0` (infinite), provide `-RecordSeconds` to avoid recording indefinitely.

## VS Code Tasks

- Commit + Record (short test): Runs a bounded commit loop with synchronized screen recording.
- Live Recorder (mss preview): Opens a preview window; press ESC to stop.
- Single Screenshot: Captures a timestamped full-screen PNG.
- Start Commit Loop (infinite, no web): Launches the external commit loop without opening Copilot via protocol if not focused.
- Stop Commit Loops: Stops running external commit loops.
- Cleanup Old Movies/Images: Runs the cleanup utility to delete old PNG/MP4 according to `config/policy_rules.json`.
 - Mark Navigation Media Assessed: Adds `.assessed` markers to `logs/screens` so the 5s cleanup can safely delete reviewed media.
 - Test/Gather/Assess Workflow: Runs navigation test, OCR commit test, observe/react, short recording (auto-marked), cleanup, and writes a workflow summary under `logs/tests/`.

## Test / Gather / Assess

- Test:
  - Navigation: verifies focusing and chat interactions
    - Command:
      ```powershell
      C:/Users/yerbr/AI_Coder_Controller/Scripts/python.exe scripts/navigation_test.py
      ```
    - Output: JSON report under `logs/tests/`.
  - OCR Smoke: sanity-check OCR pipeline
    - Command:
      ```powershell
      C:/Users/yerbr/AI_Coder_Controller/Scripts/python.exe Scripts/ocr_smoke_test.py
      ```
  - OCR Commit: captures Copilot App + VS Code Chat OCR and appends to notes
    - Command:
      ```powershell
      C:/Users/yerbr/AI_Coder_Controller/Scripts/python.exe Scripts/ocr_commit_test.py
      ```
    - Output: JSON report `logs/tests/ocr_commit_test_*.json` and appended text in `projects/Self-Improve/improvements.md`.

- Gather:
  - Commit + Record (bounded): synchronized commit loop + recording with auto-marker
    - VS Code Task: "Commit + Record (short test)"
  - Live Recorder (preview or file):
    - Example (file, marked):
      ```powershell
      C:/Users/yerbr/AI_Coder_Controller/Scripts/python.exe scripts/monitor_live.py --seconds 3 --fps 10 --out logs/screens/live_auto.mp4 --backend mss --mark-assessed
      ```
  - Screenshots / Time-lapse:
    - Single shot:
      ```powershell
      powershell -NoProfile -ExecutionPolicy Bypass -File scripts/capture_screen.ps1 -Out "logs/screens/screen_$(Get-Date -Format yyyyMMdd_HHmmss).png" -StampTime
      ```

- Assess:
  - Foreground hygiene (closes disallowed):
    - VS Code Task: "Observe & React (close disallowed)"
  - System inventory / duplicates / idle:
    - Command:
      ```powershell
      C:/Users/yerbr/AI_Coder_Controller/Scripts/python.exe scripts/assess_windows.py
      ```
  - Review OCR captures and notes:
    - Check `logs/ocr/` and `projects/Self-Improve/improvements.md`.
  - REQUIRED: Assess both errors and successes.
    - Errors: Inspect stderr tails and non-zero return codes in `logs/tests/workflow_summary_*.json` under `errors` with step names and details.
    - Successes: Confirm positive evidence (e.g., created reports, appended notes, deleted artifacts) under `successes` with paths and flags.
    - The workflow task exits non-zero on overall failure for visibility; use this to gate follow-up actions.

- Cleanup (automatic every few seconds during UI; on-demand any time):
  - Policy: `logs/screens` PNG/MP4 are deleted after 5 seconds only if a sidecar `.assessed` exists (created by recorder/wrapper or via the marking tool).
  - On-demand:
    ```powershell
    C:/Users/yerbr/AI_Coder_Controller/Scripts/python.exe Scripts/cleanup_run.py
    ```
  - Note: Some standalone scripts also run the same cleanup pass on exit; disable with `AI_CONTROLLER_RUN_CLEANUP=0`.

- One-click end-to-end:
  - VS Code Task: "Test/Gather/Assess Workflow"
  - Produces `logs/tests/workflow_summary_*.json` summarizing step results and artifacts, including PASS/FAIL per step and overall. The task returns non-zero on FAIL for visibility.

- Context pack for Copilot:
  - The Test/Gather/Assess workflow now also runs `Scripts/prepare_context_pack.py`.
  - Output: `Copilot_Attachments/ContextPack_Current.md` – a compact, linked summary of the project, objectives, policies, inventories, and the latest workflow run.
  - Recommended pattern when asking Copilot for help on this project:
    - Attach `Copilot_Attachments/ContextPack_Current.md` (and optionally related *_fs_structure.txt files).
    - In your prompt, say: "Please read ContextPack_Current.md for project context before answering." and then describe your goal.

## Image Utilities
- HTML → Image: scripts/html_to_image.py (Playwright preferred, headless Chrome/Edge fallback)
- Compose Image: scripts/compose_image.py (Pillow social-card)

## Safety & Troubleshooting
- ESC always pauses/resumes AI controls immediately.
- If OCR returns little text, tweak `targets.vscode_chat`/`targets.copilot_app` ROI and increase `chat_settle_ms`/`app_settle_ms` in config/ocr.json.
- If the Copilot window title differs (e.g., “Microsoft Copilot”), set the `-Title` parameter for commit scripts or adjust `config/policy_rules.json` `auto_commit_title`.
- If multiple PowerShell windows appear, run `scripts/close_idle_powershell.py` to close the idle one.

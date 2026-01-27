# Orchestrator Pipelines (Config-Driven)

This repo includes a small orchestration framework under `src/orchestrator/`.

- **Goal:** run small pipelines (capture → analyze → act) with consistent results/logging.
- **Safety:** runs in **dry-run by default**; you must opt in to real input actions with `--live`.
- **Portability:** the demo modules are pure-Python; vision/action modules are Windows-first but still support dry-run safely.

## Run the CLI

From the repo root:

- Dry-run (recommended first):
  - `python -m src.orchestrator.cli --config config/orchestrator_pipeline_demo.json --max-iterations 1`
- Live (will actually perform actions if the pipeline includes them):
  - `python -m src.orchestrator.cli --config config/orchestrator_pipeline_click_best_template_verify.json --max-iterations 1 --live`

Tip: in this repo you can also run with the bundled interpreter:

- `./Scripts/python.exe -m src.orchestrator.cli --config config/orchestrator_pipeline_demo.json --max-iterations 1`

## Config Shape

A pipeline config is JSON with these top-level keys:

- `root` (string): filesystem root for relative paths (default `.`)
- `pipeline` (list of strings): ordered module names to execute
- One section per module name (object): module-specific config

Example (screenshot → best match → click → verify):

```json
{
  "root": ".",
  "pipeline": ["capture_screenshot", "match_best_template", "act_click", "verify_after_click"],
  "capture_screenshot": {"monitor_index": 1, "out_dir": "logs/screens"},
  "match_best_template": {"templates_dir": "assets/ui_templates", "threshold": 0.85},
  "act_click": {"state_file": "config/controls_state.json", "owner": "orchestrator_cli"},
  "verify_after_click": {"delay_ms": 350, "disappear_threshold": 0.75}
}
```

## Default Module Registry

The CLI uses `src.orchestrator.default_registry.build_default_registry()`.

### Demo modules (safe, cross-platform)

- `capture_counter` → `CounterCapture`
- `analyze_double` → `DoublerAnalyze`
- `act_print` → `PrintAct`

### Real wrappers (Windows-first)

- `capture_record` → `ScreenRecordModule` (wraps `src.capture.ScreenCapture`)

### Vision → action

- `capture_screenshot` → `ScreenshotCaptureModule`
- `match_template` → `TemplateMatchModule`
- `match_best_template` → `BestTemplateMatchModule`
- `act_click` → `ClickMatchModule`
- `verify_after_click` → `VerifyAfterClickModule`

## Module I/O Expectations (Practical)

The runner keeps a single `data` dict that is passed from module to module. Modules write their outputs into that dict.

- `capture_screenshot` adds `data["screenshot"] = {"ok": true, "image_path": "...", "bbox": {...}}`
- matchers add `data["match"] = {"ok": true, "template_path": "...", "score": 0.93, "center_x": 123, "center_y": 456}`
- `act_click` reads `data["match"]` and emits `data["click"]` info

## Safety Model for Live Clicking

`act_click` is fail-closed and will not click unless ALL are true:

- CLI is invoked with `--live`
- A match exists (`data["match"]["ok"] == true`)
- Shared controls state allows it:
  - `paused` is false
  - If the state is fresh: `in_control_window` is true
  - If the state is fresh and an `owner` exists, it must be the same owner (`orchestrator_cli` by default)

`verify_after_click` only runs in live mode:

- It re-captures and re-matches after a short delay.
- It fails closed when evidence is weak (for example the template did not move/disappear enough).
- It does not attempt to undo the click; it’s meant as an evidence gate for higher-level workflows.

## Improving Match Reliability

If you get frequent false matches or `below_threshold`:

- Prefer restricting to a ROI:
  - Set `capture_screenshot.bbox = {"left": ..., "top": ..., "width": ..., "height": ...}`
  - Use the interactive calibrator to generate this bbox:
    - Preview only:
      - `./Scripts/python.exe Scripts/calibrate_bbox.py --monitor-index 1 --config config/orchestrator_pipeline_click_best_template.json`
    - Write into the config:
      - `./Scripts/python.exe Scripts/calibrate_bbox.py --monitor-index 1 --config config/orchestrator_pipeline_click_best_template.json --write`
    - Tips:
      - Use `--section capture_screenshot` to target a different section name.
      - Use `--clamp` to clamp the box to the monitor bounds.
      - The UI uses ENTER to confirm, ESC to cancel.
- Curate templates:
  - Put only stable UI element templates into `assets/ui_templates/curated/`
  - Lower template counts for speed (`match_best_template.max_templates`)
- Tune thresholds:
  - Start at `0.85`, then raise for safety and lower only if you understand the failure mode

## Adding a New Module

1) Implement `Module` (`init`, `run_once`, `shutdown`) under `src/orchestrator/modules/`.
2) Register it in `src/orchestrator/default_registry.py` (or build your own registry).
3) Add a config section and include it in `pipeline`.

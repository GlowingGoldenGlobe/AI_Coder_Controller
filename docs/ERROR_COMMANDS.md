# Error / Debug Commands

This project writes detailed structured JSONL events to `logs/errors/events.jsonl` (in addition to `logs/actions/actions.jsonl`).

Use these commands to quickly answer questions like:
- “Did we actually click ‘More options’ (‘+’)?”
- “What did the flyout menu show?”
- “Which candidate menu item was evaluated as Upload?”
- “Did we click/focus the File name input before pasting?”

## PowerShell: Tail and Filter

### Tail last N lines
```powershell
Get-Content -Path logs/errors/events.jsonl -Tail 400
```

### Filter for the attach workflow (common)
```powershell
$lines = Get-Content -Path logs/errors/events.jsonl -Tail 1200
$lines | Select-String -SimpleMatch \
  'copilot_app_more_options_menu_ocr',
  'copilot_app_more_options_menu_item_eval',
  'copilot_app_more_options_menu_pick',
  'copilot_app_attach_click',
  'copilot_app_attach_nav_reject',
  'copilot_app_dialog_click',
  'copilot_app_attach_opened',
  'copilot_app_attachment_failed' \
| Select-Object -Last 120 \
| ForEach-Object { $_.Line }
```

### Show only flyout image-analysis readout and which option was chosen
```powershell
$lines = Get-Content -Path logs/errors/events.jsonl -Tail 2000
$lines | Select-String -SimpleMatch \
  'copilot_app_more_options_menu_ocr',
  'copilot_app_more_options_menu_item_eval',
  'copilot_app_more_options_menu_pick' \
| ForEach-Object { $_.Line }
```

### Show only file dialog “File name” focusing/clicking
```powershell
$lines = Get-Content -Path logs/errors/events.jsonl -Tail 2000
$lines | Select-String -SimpleMatch 'copilot_app_dialog_click', 'dialog_focus_filename' \
| ForEach-Object { $_.Line }
```

## What to Look For (Key Events)

- `copilot_app_more_options_menu_ocr`
  - Contains `labels` (OCR-extracted lines when available) and `image_paths` (screenshots under `logs/ocr/`).
- `copilot_app_more_options_menu_item_eval`
  - One per candidate; includes `score`, `ocr_preview`, `image_path`.
- `copilot_app_more_options_menu_pick`
  - When a candidate is selected, includes `reason` (e.g., `icon_only_ocr_pick`) and selection metadata.
- `copilot_app_attach_click`
  - Every click attempt, with `point_image_path` evidence.
- `copilot_app_dialog_click`
  - Explicit click on the file dialog `File name` field, includes `ok` (click blocked vs allowed).

## Image Analysis / OCR Evidence

When you see an `image_path` (or `point_image_path`) in the JSON line, open that PNG from `logs/ocr/` to verify what the UI looked like at that exact step.

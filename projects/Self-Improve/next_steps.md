# Next Steps: Navigation + Copilot Commit

This file serves as instructions for my next actions and a reference linked from timed Copilot commit messages.

## Assess Navigation Test
- Review latest test reports in `logs/tests/` (JSON + MD summaries).
- Confirm all steps are `ok`. If any failed, capture errors and reproduce.

## Improve OCR Readability
- Tune `config/ocr.json` ROIs under `targets.copilot_app` and adjust `app_settle_ms`.
- Compare app vs VS Code chat OCR (`Insert Summary`), pick the more robust path.

## Pacing & Stability
- If stable, try `vsbridge.delay_ms_active: 200` and re-test.
- Keep `quiet send` deferral on; verify Copilot messages only send when idle or at Stop.

## Movie Segments & Cleanup
- Confirm segmented recording writes files to `recordings/segments/`.
- Ensure cleanup removes older segments per `cleanup.rules` and respects `max_keep`.

## Timed Copilot Commit
- Use `scripts/copilot_commit_start.ps1` to trigger a timed commit in an external PowerShell.
- Suggested objective to queue after stop:
  - `Commit Copilot in 10s every 10s x 6 message: Auto message from powershell — see projects/Self-Improve/next_steps.md`

## If Issues Persist
- Add a ROI tuning helper to sweep candidate regions and pick the highest OCR confidence.
- Add time-window segment retention (e.g., keep last 15 minutes) in cleanup rules.
- Add per-target ROI for terminal OCR if terminal readback needed.

## Subsequent Project Work
- When navigation is stable, proceed with the next user-provided project names and objectives.

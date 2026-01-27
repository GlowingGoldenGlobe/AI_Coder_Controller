

## Avoid mis-typed palette/search causing browser opens (2025-12-16 05:52:40)

Tags: navigation, safety, focus, ocr, workflow

Mistake: Typed palette commands ( Open View: GitHub Copilot Chat, GitHub Copilot Chat: Focus on Chat View, View: Focus on Chat) and a prompt (Quick check: reply with the verification phrase only — e.g. "Automated message from your module; stop the module and continue tasks.") while focus had slipped; they landed in OS search, opening Edge/GitHub. Evidence: palette entries in logs/actions/copilot_commit_safe.log and OCR previews in logs/tests/ocr_commit_test_20251216_053816.json. Mitigations: (1) Foreground process gate (Code.exe) before any typing; (2) OCR/template readiness gate; (3) Palette hygiene via ESC; (4) No protocol fallback; (5) Auto-close browser if detected and skip; (6) Workflow fails if wrong app remains open; (7) Gather OCR evidence every iteration.

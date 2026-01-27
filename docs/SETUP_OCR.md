# Enable Image Analysis for Copilot Chat (Windows)

This project can read Copilot Chat text by OCR-ing a portion of your screen. It uses Tesseract via `pytesseract`.

## Install Tesseract (system binary)

- Recommended (Windows): UB Mannheim build
  - Download: https://github.com/UB-Mannheim/tesseract/wiki
  - Install to default path: C:\\Program Files\\Tesseract-OCR\\tesseract.exe

- Or via Chocolatey (elevated PowerShell):
```powershell
choco install tesseract --yes
```

## Python packages

From your venv:
```powershell
pip install pytesseract pillow mss
```

## Configure ROI and path

Edit config/ocr.json:
```json
{
  "monitor_index": 1,
  "region_percent": { "left": 65, "top": 8, "width": 34, "height": 88 },
  "tesseract_cmd": "C:/Program Files/Tesseract-OCR/tesseract.exe",
  "tesseract_psm": 6,
  "save_debug_images": true
}
```
- Adjust region_percent to match where Copilot chat appears on your monitor.
- If Tesseract is on PATH, you can omit tesseract_cmd.

## Smoke test

- Open VS Code and bring Copilot Chat to the foreground.
- Run:
```powershell
python scripts/ocr_smoke_test.py
```
- Expected: it saves a debug image under logs/ocr/ and prints extracted text.

## Integration notes

- The image-analysis class src/ocr.py::ImageAnalyzer (aliased as CopilotOCR for compatibility) is designed to be called after focusing the Copilot chat view.
- Typical flow: VS Code automation focuses chat → image analysis captures a region + detects elements/templates → optional OCR extracts text → program appends results to projects/Self-Improve/improvements.md.

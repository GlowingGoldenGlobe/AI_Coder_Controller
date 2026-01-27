from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


def _html_uri(path: Path) -> str:
    return path.resolve().as_uri()


def render_with_playwright(html: Path, out_png: Path, width: int, height: int, full_page: bool, wait_ms: int) -> bool:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        return False

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(_html_uri(html))
        if wait_ms > 0:
            page.wait_for_timeout(wait_ms)
        page.screenshot(path=str(out_png), full_page=full_page)
        browser.close()
        return True


def _find_browser_candidates() -> list[Path]:
    candidates = []
    # Common Windows locations for Chrome/Edge
    roots = [
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
    ]
    candidates.extend([p for p in roots if p.exists()])
    # PATH-based
    for name in ("chrome", "msedge", "chromium", "chrome.exe", "msedge.exe"):
        exe = shutil.which(name)
        if exe:
            candidates.append(Path(exe))
    return candidates


def render_with_headless(html: Path, out_png: Path, width: int, height: int, full_page: bool, wait_ms: int) -> bool:
    browsers = _find_browser_candidates()
    if not browsers:
        return False
    url = _html_uri(html)
    # Some headless builds ignore full_page; we’ll prefer window size
    args = [
        "--headless=new",
        "--disable-gpu",
        f"--window-size={width},{height}",
        f"--screenshot={str(out_png)}",
    ]
    # Chromium-based allow a small delay via eval wait when needed; skip for simplicity
    try:
        subprocess.run([str(browsers[0]), *args, url], check=True)
        return out_png.exists()
    except Exception:
        return False


def html_to_image(html_path: Path, out_png: Path, width: int, height: int, full_page: bool, wait_ms: int) -> None:
    out_png.parent.mkdir(parents=True, exist_ok=True)
    # Try Playwright first
    if render_with_playwright(html_path, out_png, width, height, full_page, wait_ms):
        print(f"Rendered via Playwright → {out_png}")
        return
    # Fallback to headless browser
    if render_with_headless(html_path, out_png, width, height, full_page, wait_ms):
        print(f"Rendered via headless browser → {out_png}")
        return
    # Guidance
    print("ERROR: Could not render HTML to image. Install one of:\n"
          "  - Playwright: pip install playwright && python -m playwright install chromium\n"
          "  - Google Chrome or Microsoft Edge (headless) on PATH")


def main():
    ap = argparse.ArgumentParser(description="Render an HTML file to a PNG image")
    ap.add_argument("html", type=Path, help="Path to HTML file")
    ap.add_argument("out", type=Path, help="Output PNG path")
    ap.add_argument("--width", type=int, default=1200)
    ap.add_argument("--height", type=int, default=800)
    ap.add_argument("--full-page", action="store_true", help="Capture full page height")
    ap.add_argument("--wait-ms", type=int, default=0, help="Optional wait after load for JS to settle")
    args = ap.parse_args()
    html_to_image(args.html, args.out, args.width, args.height, args.full_page, args.wait_ms)


if __name__ == "__main__":
    main()

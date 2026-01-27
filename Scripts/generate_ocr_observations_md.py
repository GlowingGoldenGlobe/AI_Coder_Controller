import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class ImageRef:
    ts: str
    event: str
    key: str
    img_abs: str
    context: str
    payload: Dict[str, Any]


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _relpath(workspace_root: Path, p: str) -> str:
    try:
        ap = Path(p)
        rp = ap
        try:
            rp = ap.relative_to(workspace_root)
        except Exception:
            # best-effort normalize by string replacement
            ws = str(workspace_root).rstrip("\\/")
            s = str(ap)
            if s.lower().startswith(ws.lower()):
                rp = Path(s[len(ws) :].lstrip("\\/"))
        return rp.as_posix()
    except Exception:
        return str(p).replace("\\", "/")


def _pick_target_run(events: List[Dict[str, Any]], file_substr: Optional[str]) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    if file_substr:
        matching = [e for e in events if file_substr in str(e.get("file") or "")]
        if matching:
            # Use the most recent contiguous window where file matches.
            target_file = str(matching[-1].get("file") or "")
            # Find last failure for that file.
            last_idx = None
            for i in range(len(events) - 1, -1, -1):
                if str(events[i].get("file") or "") == target_file and str(events[i].get("event") or "") == "copilot_app_attachment_failed":
                    last_idx = i
                    break
            if last_idx is None:
                last_idx = len(events) - 1

            # Find start marker (attempted) for that file.
            start_idx = 0
            for i in range(last_idx, -1, -1):
                if str(events[i].get("event") or "") == "copilot_app_attachment_attempted" and str(events[i].get("file") or "") == target_file:
                    start_idx = i
                    break
            window = [e for e in events[start_idx : last_idx + 1] if str(e.get("event") or "").startswith("copilot_")]
            return target_file, window

    # Fallback: use the most recent copilot attach window (no file filter)
    last_idx = None
    for i in range(len(events) - 1, -1, -1):
        if str(events[i].get("event") or "").startswith("copilot_"):
            last_idx = i
            break
    if last_idx is None:
        return None, []

    start_idx = 0
    for i in range(last_idx, -1, -1):
        if str(events[i].get("event") or "") == "copilot_app_attachment_attempted":
            start_idx = i
            break
    window = [e for e in events[start_idx : last_idx + 1] if str(e.get("event") or "").startswith("copilot_")]
    return None, window


def _extract_images(window: List[Dict[str, Any]]) -> List[ImageRef]:
    refs: List[ImageRef] = []
    for e in window:
        ts = str(e.get("ts") or "")
        ev = str(e.get("event") or "")
        ctx = str(e.get("tag") or e.get("step") or e.get("name") or e.get("target") or "")

        for key in ("point_image_path", "image_path"):
            v = e.get(key)
            if isinstance(v, str) and v.lower().endswith(".png"):
                refs.append(ImageRef(ts=ts, event=ev, key=key, img_abs=v, context=ctx, payload=e))

        v2 = e.get("image_paths")
        if isinstance(v2, list):
            for i, p in enumerate(v2):
                if isinstance(p, str) and p.lower().endswith(".png"):
                    refs.append(ImageRef(ts=ts, event=ev, key=f"image_paths[{i}]", img_abs=p, context=ctx, payload=e))

    # dedup by absolute path, preserve order
    seen = set()
    uniq: List[ImageRef] = []
    for r in refs:
        if r.img_abs in seen:
            continue
        seen.add(r.img_abs)
        uniq.append(r)
    return uniq


def _guess_needed_objects(ev: str, payload: Dict[str, Any]) -> List[str]:
    tag = str(payload.get("tag") or payload.get("step") or "").lower()
    prev = (str(payload.get("point_preview") or payload.get("preview") or payload.get("ocr_preview") or "").lower())

    if "input_plus_more_options" in tag or "more_options" in tag and "upload" not in tag:
        return ["More options (+) button", "More options flyout/menu"]
    if "more_options_upload" in tag or ("upload" in prev):
        return ["Upload menu item", "File picker (Open dialog)"]
    if "dialog" in ev or "file_picker" in ev:
        return ["File name input field", "Open button", "Folder/address bar"]
    if "hotspot" in ev:
        return ["Attach/+ area", "File picker elements (Open/Cancel)"]
    return ["Relevant UI control for next step"]


def _cursor_correctness(payload: Dict[str, Any]) -> str:
    # We can't view the image pixels here; we use the point OCR preview & tags as evidence.
    tag = str(payload.get("tag") or payload.get("step") or "")
    point_preview = str(payload.get("point_preview") or "")
    if not tag and not point_preview:
        return "Unknown (no point OCR preview recorded for this image)"

    if "more_options_upload" in tag.lower():
        if "upload" in point_preview.lower():
            return "Yes — point OCR includes 'Upload', consistent with hovering/clicking the Upload menu item."
        return "Unclear — tag indicates Upload target, but point OCR preview does not clearly contain 'Upload'."

    if "input_plus_more_options" in tag.lower() or "more_options" == tag.lower():
        if "more options" in point_preview.lower() or "+" in point_preview:
            return "Yes — point OCR indicates the '+' / 'More options' affordance near the input."
        return "Unclear — tag indicates More options, but point OCR preview is weak/empty."

    if "mouse_hotspot" in tag.lower():
        if "open" in point_preview.lower() or "cancel" in point_preview.lower():
            return "Likely yes — point OCR shows 'Open'/'Cancel', consistent with hovering a file picker surface."
        return "Unclear — hotspot target without strong point OCR confirmation."

    if "file name" in point_preview.lower() or "filename" in point_preview.lower():
        return "Yes — point OCR references the File name field."

    return "Unclear — insufficient OCR evidence to confirm the cursor is on the intended object."


def _intended_next_action(payload: Dict[str, Any]) -> str:
    tag = str(payload.get("tag") or payload.get("step") or "").lower()
    point_preview = str(payload.get("point_preview") or "").lower()

    if "more_options" in tag and "upload" not in tag:
        return "Open the flyout and select the Upload/Add files action based on OCR evidence."
    if "upload" in tag or "upload" in point_preview:
        return "Wait for the file picker and focus 'File name' (Alt+N), paste the full path, then press Enter/Open."
    if "open" in point_preview or "cancel" in point_preview:
        return "Treat the foreground as the file picker, focus 'File name' (Alt+N), paste the full path, and confirm (Enter)."
    return "Proceed to the next UI step indicated by the observed control." 


def _previous_action(window: List[Dict[str, Any]], idx: int) -> str:
    # Walk backwards for the last click/key/type.
    for j in range(idx - 1, -1, -1):
        ev = str(window[j].get("event") or "")
        if ev in {
            "copilot_app_attach_click",
            "copilot_app_attach_key",
            "copilot_app_attach_type",
            "copilot_app_dialog_click",
            "copilot_app_more_options_menu_pick",
        }:
            tag = str(window[j].get("tag") or window[j].get("step") or window[j].get("name") or "")
            return f"{ev} ({tag})"
    return "Unknown (no prior action event found in window)"


def _location_expected(payload: Dict[str, Any]) -> str:
    tag = str(payload.get("tag") or payload.get("step") or "").lower()
    point_preview = str(payload.get("point_preview") or "").lower()

    if "more_options" in tag and "upload" not in tag:
        return "Yes — expected to be near the chat input where the '+' / More options lives."
    if "more_options_upload" in tag:
        return "Yes — expected to be in the More options flyout where Upload appears."
    if "open" in point_preview or "cancel" in point_preview:
        return "Yes — expected once the file picker opens."
    return "Unclear — the tag/preview does not uniquely identify an expected region."


def _present_check(needed: List[str], payload: Dict[str, Any]) -> List[Tuple[str, bool, str]]:
    # Use OCR previews as evidence.
    text = "\n".join(
        [
            str(payload.get("point_preview") or ""),
            str(payload.get("preview") or ""),
            str(payload.get("ocr_preview") or ""),
            "\n".join(payload.get("labels") or []) if isinstance(payload.get("labels"), list) else "",
        ]
    ).lower()
    out = []
    for obj in needed:
        key = obj.lower()
        hit = False
        evidence = ""
        if "upload" in key:
            hit = "upload" in text
            evidence = "contains 'upload'" if hit else "no 'upload' found"
        elif "more options" in key or "+" in obj:
            hit = "more options" in text or "+" in text
            evidence = "contains '+' or 'more options'" if hit else "no '+'/'more options' found"
        elif "file name" in key:
            hit = "file name" in text or "filename" in text
            evidence = "contains 'file name'" if hit else "no 'file name' found"
        elif "open" in key:
            hit = "open" in text
            evidence = "contains 'open'" if hit else "no 'open' found"
        else:
            # generic heuristic
            tokens = [t for t in key.replace("(", " ").replace(")", " ").split() if len(t) >= 4]
            hit = any(t in text for t in tokens)
            evidence = "token match" if hit else "no token match"
        out.append((obj, hit, evidence))
    return out


def generate_md(
    workspace_root: Path,
    events_path: Path,
    out_path: Path,
    file_substr: Optional[str],
) -> Path:
    events = _load_jsonl(events_path)
    target_file, window = _pick_target_run(events, file_substr)
    imgs = _extract_images(window)

    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines: List[str] = []
    lines.append("# OCR Image Observations")
    # Detect repeated navigation/menu/hotspot events (simple heuristic)
    def _find_repeats(win: List[Dict[str, Any]], threshold: int = 2) -> List[Dict[str, Any]]:
        repeats: List[Dict[str, Any]] = []
        last_key = None
        count = 0
        start_idx = 0
        for i, e in enumerate(win):
            ev = str(e.get("event") or "")
            tag = str(e.get("step") or e.get("tag") or "")
            # We consider attach_observe variants and hotspot/menu steps as navigation
            key = None
            if ev.startswith("copilot_app_attach_observe"):
                key = tag or ev
            elif "menu_down" in ev or "menu" in tag.lower() or "hotspot" in tag.lower():
                key = tag or ev

            if key is None:
                # reset
                if count >= threshold and last_key:
                    repeats.append({"start": start_idx, "end": i - 1, "key": last_key, "count": count})
                last_key = None
                count = 0
                continue

            if key == last_key:
                count += 1
            else:
                if count >= threshold and last_key:
                    repeats.append({"start": start_idx, "end": i - 1, "key": last_key, "count": count})
                last_key = key
                count = 1
                start_idx = i

        # flush
        if count >= threshold and last_key:
            repeats.append({"start": start_idx, "end": len(win) - 1, "key": last_key, "count": count})
        return repeats

    repeats = _find_repeats(window, threshold=2)
    lines.append("")
    lines.append(f"- Source log: `{events_path.as_posix()}`")
    if target_file:
        lines.append(f"- Target file: `{target_file}`")
    if file_substr:
        lines.append(f"- Filter: `{file_substr}`")
    lines.append(f"- Images referenced in run window: **{len(imgs)}**")
    lines.append("")
    # If we detected repeated navigation-like steps, call them out so reviewers notice potential loops.
    if repeats:
        lines.append("## Detected repeated navigation / menu hops")
        lines.append("")
        lines.append("The run contains consecutive repeated navigation/menu/hotspot observations which may indicate the agent navigated the same options repeatedly instead of committing. See examples below.")
        lines.append("")
        for r in repeats:
            s = window[r["start"]]
            e = window[r["end"]]
            ts_s = str(s.get("ts") or "")
            ts_e = str(e.get("ts") or "")
            lines.append(f"- `{r['key']}` repeated {r['count']} times — window {ts_s} → {ts_e}")
            # include up to 3 sample events from the repeat window
            sample_lines = []
            for j in range(r["start"], min(r["end"] + 1, r["start"] + 3)):
                ev = window[j]
                sample_lines.append(f"  - {ev.get('ts')} {ev.get('event')} {ev.get('step') or ev.get('tag') or ''}")
            lines.extend(sample_lines)
        lines.append("")
    lines.append("## Re-run")
    lines.append("")
    # Keep this copy/pastable in PowerShell and cmd.
    example_out = _relpath(workspace_root, str(out_path))
    example_events = _relpath(workspace_root, str(events_path))
    example_file = (file_substr or "<substring>")
    lines.append("Use this to regenerate the report for a different run or output path:")
    lines.append("")
    lines.append("```powershell")
    lines.append(
        "C:/Users/yerbr/AI_Coder_Controller/Scripts/python.exe Scripts/generate_ocr_observations_md.py "
        f"--events {example_events} --out {example_out} --file-substr {example_file}"
    )
    lines.append("```")
    lines.append("")
    lines.append("## How to use")
    lines.append("")
    lines.append("Open each linked image and compare with the OCR preview text recorded next to it.")
    lines.append("")

    # Build an index for previous action lookup.
    index_by_img = {r.img_abs: i for i, r in enumerate(imgs)}

    for idx, ref in enumerate(imgs, start=1):
        rel = _relpath(workspace_root, ref.img_abs)
        payload = ref.payload
        needed = _guess_needed_objects(ref.event, payload)
        present = _present_check(needed, payload)

        # Find the payload position within the window to locate previous action.
        # We approximate by searching the first matching ts+event+img.
        win_idx = None
        for j, e in enumerate(window):
            if str(e.get("ts") or "") == ref.ts and str(e.get("event") or "") == ref.event:
                if str(e.get(ref.key) or "") == ref.img_abs or (ref.key.startswith("image_paths") and ref.img_abs in (e.get("image_paths") or [])):
                    win_idx = j
                    break
        prev_action = _previous_action(window, win_idx or 0)

        lines.append(f"## {idx}. {ref.event} — {ref.context} — {ref.ts}")
        lines.append("")
        lines.append(f"**Image:** [{rel}]({rel})")
        lines.append("")

        # (1) objects needed
        lines.append("### (1) Needed objects present?")
        for obj, ok, evidence in present:
            lines.append(f"- {obj}: {'YES' if ok else 'NO/UNCLEAR'} ({evidence})")
        lines.append("")

        # (2) cursor hovering correct object
        lines.append("### (2) Cursor hovering correct object?")
        lines.append(f"- { _cursor_correctness(payload) }")
        lines.append("")

        # (3) intended next action
        lines.append("### (3) Intended next action")
        lines.append(f"- { _intended_next_action(payload) }")
        lines.append("")

        # (4) previous action
        lines.append("### (4) Previous action that led here")
        lines.append(f"- {prev_action}")
        lines.append("")

        # (5) expected location
        lines.append("### (5) Was this the location expected?")
        lines.append(f"- { _location_expected(payload) }")
        lines.append("")

        # (6) anything else
        lines.append("### (6) Notes / anything else")
        # Include compact evidence fields
        point_preview = str(payload.get("point_preview") or "").strip()
        preview = str(payload.get("preview") or "").strip()
        ocr_preview = str(payload.get("ocr_preview") or "").strip()
        labels = payload.get("labels") if isinstance(payload.get("labels"), list) else None

        if point_preview:
            lines.append(f"- point_preview: {point_preview[:260]}")
        if preview and preview != point_preview:
            lines.append(f"- preview: {preview[:260]}")
        if ocr_preview and ocr_preview not in (preview, point_preview):
            lines.append(f"- ocr_preview: {ocr_preview[:260]}")
        if labels:
            # Show a short list to avoid huge sections
            sample = [str(x) for x in labels[:10] if str(x).strip()]
            if sample:
                lines.append(f"- labels(sample): {sample}")
        # Include any probe info if present
        for k in ("probe_name", "probe_control_type", "reason", "target", "step", "tag"):
            if payload.get(k) not in (None, ""):
                lines.append(f"- {k}: {str(payload.get(k))[:180]}")
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", default="logs/errors/events.jsonl")
    ap.add_argument("--out", default="Archive_OCR_Images_Assessments/OBSERVED_OCR_IMAGES.md")
    ap.add_argument("--file-substr", default="ModulesList_fs_agent_20251218_013825.txt")
    ap.add_argument("--workspace-root", default=str(Path.cwd()))
    args = ap.parse_args()

    out = generate_md(
        workspace_root=Path(args.workspace_root).resolve(),
        events_path=Path(args.events),
        out_path=Path(args.out),
        file_substr=str(args.file_substr) if args.file_substr else None,
    )
    print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

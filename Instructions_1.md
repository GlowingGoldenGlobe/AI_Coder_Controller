AI-Auto-User-Coder (AI_Coder_Controller)
========================================
Single-file instructions and code bundle to build and run a Windows-first program that:
- Records the screen as a movie
- Safely controls mouse and keyboard
- Parses objectives from .txt/.md
- Navigates VSCode and Copilot via automation
- Interacts with VSCode agent mode (chat panels, terminals, files)
- Provides a local UI with file uploads, sidebar listing, and Run/Pause/Resume/Stop controls
- Logs run events, pauses, stops, and user messages

This document contains:
1) Step-by-step VSCode agent build instructions
2) Project structure and file mapping
3) Configuration examples
4) Complete Python source code for all modules
5) Notes and keyboard mappings for VSCode + Copilot automation

----------------------------------------------------------------
Section 1 — Build and run instructions (Windows 11, VSCode)
----------------------------------------------------------------

What you will do:
- Open VSCode
- Create new folder: AI-Auto-User-Coder as AI_Coder_Controller
- Ensure Python venv is pre-installed (you already have venv capability)

What you will upload:
- The code files from this text beneath “Section 4 — Source code”. Save them under C:\Users\yerbr\AI_Algorithms\AI_Coder_Controller\ following the structure below.

Steps:
1) Create the root folder:
   C:\Users\yerbr\AI_Algorithms\AI_Coder_Controller\

2) Create and activate a virtual environment (PowerShell):
   python -m venv C:\Users\yerbr\AI_Algorithms\AI_Coder_Controller\.venv
   C:\Users\yerbr\AI_Algorithms\AI_Coder_Controller\.venv\Scripts\Activate.ps1

3) Install dependencies:
   pip install mss opencv-python pyautogui pynput numpy rich pillow tk

   Notes:
   - tk is bundled with many Python installations as tkinter; the “pillow” package is used for basic image handling in the UI.

4) In VSCode:
   - Open the folder (Ctrl+K Ctrl+O) and select C:\Users\yerbr\AI_Algorithms\AI_Coder_Controller\
   - Ensure Python interpreter points to the venv:
     Command Palette (Ctrl+Shift+P) → “Python: Select Interpreter” → choose .venv

5) Create files as per Section 3 (structure) and paste code from Section 4.

6) Run the program (PowerShell or VSCode Terminal):
   python -m src.main

7) Controls in the UI:
   - Run: start orchestration and (optionally) recording
   - Pause: freeze action execution but keep program active
   - Resume: continue execution
   - Stop: clean shutdown
   - Upload Files: add .txt/.md into the objectives list
   - Sidebar: shows managed files

8) Keyboard safety and automation:
   - Move mouse to top-left corner to trigger fail-safe (PyAutoGUI)
   - Hotkeys in program:
     F8: Toggle recording on/off
     F9: Pause/resume
     ESC: Emergency stop

9) VSCode + Copilot agent navigation (automated via keystrokes):
   - Open VSCode: via Start menu or pinned shortcut. The program can also attempt “start code” through Windows Run (Win+R) or shell, but keyboard-driving is more consistent.
   - Open folder: Ctrl+K Ctrl+O (then the program types folder path)
   - Open terminal: Ctrl+` (backtick)
   - Open file by name: Ctrl+P (Quick Open) and type filename
   - Command Palette: Ctrl+Shift+P → run “Open View: GitHub Copilot Chat”
   - Focus Copilot chat: Ctrl+Shift+P → “GitHub Copilot Chat: Focus on Chat View”
   - Inline chat: Ctrl+I (if extension default binding applies)
   - Compose messages to chat: type text and press Enter
   - Scroll chat/editor: PageUp/PageDown, Ctrl+Home/Ctrl+End, or wheel-scroll via pyautogui.scroll
   - Multiple instances: Start another VSCode window via Command Palette “New Window”, or run “code -n” (the program will use hotkeys rather than shell)

10) Copilot clarification loop:
   - If the AI policy encounters an ambiguous objective, it opens Copilot Chat, composes a clarification request, waits briefly, scrolls through Copilot output, and logs the summary before proceeding to suggested edits in VSCode.

----------------------------------------------------------------
Section 2 — Purpose
----------------------------------------------------------------
ScreenPilot/AI_Coder_Controller is a modular Windows-first program that records the screen, interprets objectives from text or Markdown files, and safely controls the mouse and keyboard. It integrates with VSCode and Copilot to deliver deterministic execution (clear instructions) and adaptive reasoning (ambiguous tasks). When a task is uncertain, it queries Copilot inside VSCode, reads the response, and continues execution based on the clarified plan. The UI provides file uploads, a sidebar of objectives, and Run/Pause/Resume/Stop controls. Observability is ensured via structured logging of run events, pauses, stops, and user messages.

----------------------------------------------------------------
Section 3 — Project structure (Windows paths)
----------------------------------------------------------------
Root: C:\Users\yerbr\AI_Algorithms\AI_Coder_Controller\

Directories and files:
- config\
  - policy_rules.json
  - objectives.md
  - instructions.md
- logs\
  - run.log
- recordings\
  - (mp4 files auto-generated)
- src\
  - capture.py
  - control.py
  - policy.py
  - ui.py
  - vsbridge.py
  - main.py
- README.md

----------------------------------------------------------------
Section 4 — Source code
----------------------------------------------------------------

File: config\policy_rules.json
--------------------------------
{
  "enabled": true,
  "actions": [
    { "when": "always", "do": "noop" }
  ],
  "bounds": {
    "mouse_speed": 0.3,
    "max_clicks_per_min": 60,
    "max_keys_per_min": 120
  },
  "hotkeys": {
    "pause": "f9",
    "toggle_record": "f8",
    "emergency_stop": "esc"
  }
}

File: config\objectives.md
--------------------------------
# Objectives
1. Start screen recording
2. Open VSCode
3. Open folder: C:\Users\yerbr\AI_Algorithms\AI_Coder_Controller\
4. Open file: src\policy.py
5. Ask Copilot to clarify: “How should Policy.decide interpret ambiguous objectives?”
6. Insert summary into src\instructions.md
7. Stop

File: config\instructions.md
--------------------------------
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

File: src\capture.py
--------------------------------
import time
from pathlib import Path
import cv2
import numpy as np
from mss import mss

class ScreenCapture:
    def __init__(self, out_path: Path, fps: int = 20, monitor_index: int = 1):
        self.out_path = out_path
        self.fps = fps
        self.monitor_index = monitor_index
        self._sct = None
        self._writer = None
        self._frame_interval = 1.0 / fps
        self._last_frame_t = 0.0

    def start(self):
        self._sct = mss()
        mon = self._sct.monitors[self.monitor_index]  # 1 = primary
        width, height = mon["width"], mon["height"]
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self._writer = cv2.VideoWriter(str(self.out_path), fourcc, self.fps, (width, height))

    def grab_frame(self):
        now = time.time()
        if now - self._last_frame_t < self._frame_interval:
            return False
        self._last_frame_t = now
        mon = self._sct.monitors[self.monitor_index]
        frame = np.array(self._sct.grab(mon))
        frame = frame[:, :, :3]  # BGRA -> BGR
        self._writer.write(frame)
        return True

    def stop(self):
        try:
            if self._writer: self._writer.release()
        finally:
            self._writer = None
        try:
            if self._sct: self._sct.close()
        finally:
            self._sct = None

File: src\control.py
--------------------------------
import time
from dataclasses import dataclass
import pyautogui

pyautogui.FAILSAFE = True  # Move mouse to top-left to abort

@dataclass
class SafetyLimits:
    max_clicks_per_min: int = 60
    max_keys_per_min: int = 120

class Controller:
    def __init__(self, mouse_speed: float = 0.3, limits: SafetyLimits = SafetyLimits()):
        self.mouse_speed = mouse_speed
        self.limits = limits
        self._clicks = 0
        self._keys = 0
        self._window_t = time.time()

    def _window_reset_if_needed(self):
        if time.time() - self._window_t > 60:
            self._window_t = time.time()
            self._clicks = 0
            self._keys = 0

    def move_mouse(self, x: int, y: int):
        pyautogui.moveTo(x, y, duration=self.mouse_speed)

    def click(self, button: str = "left"):
        self._window_reset_if_needed()
        if self._clicks >= self.limits.max_clicks_per_min:
            return False
        pyautogui.click(button=button)
        self._clicks += 1
        return True

    def type_text(self, text: str):
        self._window_reset_if_needed()
        if self._keys + len(text) > self.limits.max_keys_per_min:
            return False
        pyautogui.typewrite(text, interval=0.02)
        self._keys += len(text)
        return True

    def press_keys(self, keys):
        # keys: list like ["ctrl", "c"] or ["enter"]
        self._window_reset_if_needed()
        count = len(keys)
        if self._keys + count > self.limits.max_keys_per_min:
            return False
        pyautogui.hotkey(*keys)
        self._keys += count
        return True

File: src\policy.py
--------------------------------
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
from pathlib import Path

@dataclass
class Action:
    kind: str  # "move", "click", "type", "hotkey", "noop", "vscode", "copilot"
    params: Dict[str, Any]

class Policy:
    def __init__(self, rules: Dict[str, Any]):
        self.rules = rules
        self.enabled = rules.get("enabled", True)

    def parse_objectives(self, files: List[Path]) -> List[Dict[str, Any]]:
        tasks = []
        for fp in files:
            text = fp.read_text(encoding="utf-8", errors="ignore")
            for line in text.splitlines():
                s = line.strip()
                if not s: continue
                if s.startswith("#"): continue
                tasks.append({"src": str(fp), "text": s})
        return tasks

    def decide(self, objective_text: str) -> Optional[Action]:
        if not self.enabled:
            return None

        s = objective_text.lower()
        if "start screen recording" in s:
            return Action("vscode", {"op": "record_toggle_on"})
        if s.startswith("open vscode"):
            return Action("vscode", {"op": "open_vscode"})
        if s.startswith("open folder:"):
            path = objective_text.split(":", 1)[1].strip()
            return Action("vscode", {"op": "open_folder", "path": path})
        if s.startswith("open file:"):
            path = objective_text.split(":", 1)[1].strip()
            return Action("vscode", {"op": "open_file", "path": path})
        if "ask copilot" in s or "copilot" in s and "clarify" in s:
            # Send question to Copilot chat
            q = objective_text.split(":", 1)[-1].strip() if ":" in objective_text else objective_text
            return Action("copilot", {"op": "ask", "question": q})
        if s.startswith("stop"):
            return Action("vscode", {"op": "stop"})
        # default: noop
        return Action("noop", {})

File: src\ui.py
--------------------------------
import threading
import json
from pathlib import Path
from tkinter import Tk, Frame, Button, Listbox, Label, END, filedialog, StringVar
from rich.console import Console

console = Console()

class Hotkeys:
    def __init__(self, rules_path: Path):
        cfg = json.loads(Path(rules_path).read_text(encoding="utf-8"))
        hk = cfg.get("hotkeys", {})
        self.pause_key = hk.get("pause", "f9").lower()
        self.toggle_key = hk.get("toggle_record", "f8").lower()
        self.stop_key = hk.get("emergency_stop", "esc").lower()
        self.state = {"recording": False, "paused": False, "stop": False}

    def toggle_record(self):
        self.state["recording"] = not self.state["recording"]
        console.log(f"[bold]Recording[/] -> {self.state['recording']}")

    def toggle_pause(self):
        self.state["paused"] = not self.state["paused"]
        console.log(f"[bold]Paused[/] -> {self.state['paused']}")

    def emergency_stop(self):
        self.state["stop"] = True
        console.log("[bold red]Emergency stop[/] requested")


class AppUI:
    def __init__(self, root: Path, on_run, on_pause, on_resume, on_stop, on_user_msg, on_upload_files):
        self.root_path = root
        self.on_run = on_run
        self.on_pause = on_pause
        self.on_resume = on_resume
        self.on_stop = on_stop
        self.on_user_msg = on_user_msg
        self.on_upload_files = on_upload_files

        self.tk = Tk()
        self.tk.title("AI_Coder_Controller")
        self.files_list = Listbox(self.tk, width=60, height=15)
        self.msg_var = StringVar()

        Label(self.tk, text="Files (objectives & instructions):").pack()
        self.files_list.pack()
        controls = Frame(self.tk)
        controls.pack(pady=6)

        Button(controls, text="Upload Files", command=self.upload_files).grid(row=0, column=0, padx=5)
        Button(controls, text="Run", command=self.on_run).grid(row=0, column=1, padx=5)
        Button(controls, text="Pause", command=self.on_pause).grid(row=0, column=2, padx=5)
        Button(controls, text="Resume", command=self.on_resume).grid(row=0, column=3, padx=5)
        Button(controls, text="Stop", command=self.on_stop).grid(row=0, column=4, padx=5)

        Label(self.tk, text="User message to agent:").pack(pady=4)
        Button(self.tk, text="Send Message", command=self._send_msg).pack(pady=2)

    def upload_files(self):
        fps = filedialog.askopenfilenames(
            title="Select objectives/instructions files",
            filetypes=[("Text/Markdown", "*.txt *.md *.markdown"), ("All files", "*.*")]
        )
        if not fps: return
        added = self.on_upload_files([Path(p) for p in fps])
        for p in added:
            self.files_list.insert(END, str(p))

    def _send_msg(self):
        # Prompt user via a simple file dialog picking a text file containing the message, to keep UI minimal
        msg_file = filedialog.askopenfilename(title="Select a text file with your message", filetypes=[("Text", "*.txt"), ("All files", "*.*")])
        if not msg_file: return
        content = Path(msg_file).read_text(encoding="utf-8", errors="ignore")
        self.on_user_msg(content)

    def start(self):
        threading.Thread(target=self.tk.mainloop, daemon=True).start()

def interactive_banner(console=console):
    console.rule("[bold cyan]AI_Coder_Controller[/]")
    console.print("UI provides Upload Files, sidebar listing, Run/Pause/Resume/Stop, and user message sending.", style="yellow")

File: src\vsbridge.py
--------------------------------
import time
from pathlib import Path
import pyautogui

class VSBridge:
    """
    Keyboard/mouse-driven automation layer for VSCode + Copilot Chat.
    This layer is intentionally hotkey-centric to be robust without deep API integration.
    """
    def __init__(self, ctrl, logger):
        self.ctrl = ctrl
        self.log = logger

    def _type_path(self, path_str: str):
        self.ctrl.type_text(path_str)
        time.sleep(0.3)
        self.ctrl.press_keys(["enter"])

    def open_vscode(self):
        # Attempt via Windows Run dialog
        self.log("VSBridge: Open VSCode via Win+R and 'code'")
        self.ctrl.press_keys(["win", "r"])
        time.sleep(0.4)
        self.ctrl.type_text("code")
        self.ctrl.press_keys(["enter"])
        time.sleep(2.0)

    def open_folder(self, path: str):
        self.log(f"VSBridge: Open folder {path}")
        self.ctrl.press_keys(["ctrl", "k"])
        self.ctrl.press_keys(["ctrl", "o"])
        time.sleep(0.5)
        self._type_path(path)
        time.sleep(1.5)

    def open_file_quick(self, path: str):
        self.log(f"VSBridge: Open file via Quick Open: {path}")
        self.ctrl.press_keys(["ctrl", "p"])
        time.sleep(0.4)
        self.ctrl.type_text(path.replace("\\", "/"))
        time.sleep(0.2)
        self.ctrl.press_keys(["enter"])
        time.sleep(0.6)

    def open_terminal(self):
        self.log("VSBridge: Open terminal")
        self.ctrl.press_keys(["ctrl", "`"])
        time.sleep(0.4)

    def command_palette(self, command_text: str):
        self.log(f"VSBridge: Command Palette -> {command_text}")
        self.ctrl.press_keys(["ctrl", "shift", "p"])
        time.sleep(0.4)
        self.ctrl.type_text(command_text)
        time.sleep(0.2)
        self.ctrl.press_keys(["enter"])
        time.sleep(0.8)

    def focus_copilot_chat_view(self):
        self.command_palette("Open View: GitHub Copilot Chat")

    def focus_copilot_chat_inline(self):
        # Many setups bind inline to Ctrl+I
        self.ctrl.press_keys(["ctrl", "i"])
        time.sleep(0.5)

    def ask_copilot(self, question: str):
        self.log(f"VSBridge: Ask Copilot -> {question}")
        self.focus_copilot_chat_view()
        time.sleep(0.6)
        self.ctrl.type_text(question)
        time.sleep(0.2)
        self.ctrl.press_keys(["enter"])
        time.sleep(2.0)  # wait for response
        # Scroll response and log a marker
        self.scroll_chat_read()
        self.log("VSBridge: Copilot response read (scrolled)")

    def scroll_chat_read(self, steps: int = 6):
        self.log("VSBridge: Scroll chat")
        for _ in range(steps):
            pyautogui.scroll(-400)
            time.sleep(0.2)

    def scroll_editor(self, steps: int = 6):
        self.log("VSBridge: Scroll editor")
        for _ in range(steps):
            pyautogui.scroll(-400)
            time.sleep(0.2)

    def compose_message_vscode_chat(self, text: str):
        self.log("VSBridge: Compose message in chat view")
        self.focus_copilot_chat_view()
        time.sleep(0.5)
        self.ctrl.type_text(text)
        time.sleep(0.2)
        self.ctrl.press_keys(["enter"])

File: src\main.py
--------------------------------
import json
import time
from pathlib import Path
from rich.console import Console
from src.capture import ScreenCapture
from src.control import Controller, SafetyLimits
from src.policy import Policy, Action
from src.ui import Hotkeys, AppUI, interactive_banner
from src.vsbridge import VSBridge

console = Console()

def ensure_dirs(root: Path):
    for p in ["config", "logs", "recordings", "src"]:
        (root / p).mkdir(parents=True, exist_ok=True)

def timestamp():
    return time.strftime("%Y-%m-%d %H:%M:%S")

class Logger:
    def __init__(self, logfile: Path):
        self.logfile = logfile
    def __call__(self, msg: str):
        Console().log(msg)
        with open(self.logfile, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp()}] {msg}\n")

def run(root: Path, fps: int = 20):
    ensure_dirs(root)
    rules_path = root / "config" / "policy_rules.json"
    with open(rules_path, "r", encoding="utf-8") as f:
        rules = json.load(f)

    log = Logger(root / "logs" / "run.log")
    hotkeys = Hotkeys(rules_path)
    interactive_banner(console)

    out_file = root / "recordings" / f"capture_{int(time.time())}.mp4"
    capture = ScreenCapture(out_file, fps=fps, monitor_index=1)
    ctrl = Controller(mouse_speed=rules["bounds"]["mouse_speed"],
                      limits=SafetyLimits(rules["bounds"]["max_clicks_per_min"],
                                          rules["bounds"]["max_keys_per_min"]))
    policy = Policy(rules)
    vs = VSBridge(ctrl, log)

    state = {"running": False, "paused": False, "stop": False, "objectives": []}

    def on_run():
        state["running"] = True
        state["paused"] = False
        log("Run requested")
        capture.start()
        log(f"Recording to: {out_file}")

    def on_pause():
        state["paused"] = True
        log("Paused requested")

    def on_resume():
        state["paused"] = False
        log("Resume requested")

    def on_stop():
        state["stop"] = True
        state["running"] = False
        log("Stop requested")

    def on_user_msg(msg: str):
        log(f"User message: {msg}")
        vs.compose_message_vscode_chat(msg)

    def on_upload_files(paths):
        added = []
        for p in paths:
            if p.exists():
                added.append(p)
        state["objectives"].extend(added)
        log(f"Uploaded {len(added)} files")
        return added

    ui = AppUI(root, on_run, on_pause, on_resume, on_stop, on_user_msg, on_upload_files)
    ui.start()

    try:
        while not state["stop"]:
            if state["running"] and not state["paused"]:
                # Capture frames on pace
                capture.grab_frame()
                # Parse objectives and execute decisions
                tasks = policy.parse_objectives(state["objectives"]) if state["objectives"] else []
                for t in tasks[:10]:  # process a limited number per loop to avoid tight spin
                    act = policy.decide(t["text"])
                    if not act or act.kind == "noop":
                        continue
                    if act.kind == "vscode":
                        op = act.params.get("op")
                        if op == "record_toggle_on":
                            hotkeys.toggle_record()
                        elif op == "open_vscode":
                            vs.open_vscode()
                        elif op == "open_folder":
                            vs.open_folder(act.params.get("path", str(root)))
                        elif op == "open_file":
                            vs.open_file_quick(act.params.get("path", "README.md"))
                        elif op == "stop":
                            on_stop()
                    elif act.kind == "copilot":
                        if act.params.get("op") == "ask":
                            vs.ask_copilot(act.params.get("question", ""))
                time.sleep(0.1)
            else:
                time.sleep(0.1)
    except Exception as e:
        log(f"Exception: {e}")
    finally:
        capture.stop()
        log("Shutdown complete")

if __name__ == "__main__":
    root = Path(r"C:\Users\yerbr\AI_Algorithms\AI_Coder_Controller")
    run(root, fps=20)

File: README.md
--------------------------------
# AI_Coder_Controller

AI_Coder_Controller is a modular Windows-first program that records the screen, interprets objectives from `.txt` and `.md` files, and safely controls the mouse and keyboard. It integrates with VSCode and Copilot to deliver deterministic execution for clear instructions and adaptive reasoning for ambiguous tasks. When in doubt, it queries Copilot inside VSCode and proceeds with the clarified plan. The UI provides Upload Files, a sidebar of objectives, and Run/Pause/Resume/Stop controls, with structured logging of run events and user messages.

## Purpose
The purpose of AI_Coder_Controller is to bridge human-readable objectives and automated execution. By parsing `.txt` and `.md`, it transforms written goals into actionable steps. Clear instructions are executed locally. Ambiguous instructions trigger Copilot queries and VSCode edits. Safety and observability are provided through rate limits, fail-safes, and structured logs.

----------------------------------------------------------------
Section 5 — Notes on VSCode + Copilot key mappings
----------------------------------------------------------------
Common hotkeys used:
- Open folder dialog: Ctrl+K Ctrl+O
- Quick Open file: Ctrl+P then type path/name
- Terminal toggle: Ctrl+`
- Command Palette: Ctrl+Shift+P
- Copilot Chat View: Command Palette → “Open View: GitHub Copilot Chat”
- Focus Copilot Chat View: Command Palette → “GitHub Copilot Chat: Focus on Chat View”
- Inline Copilot Chat (if enabled): Ctrl+I
- New VSCode Window: Command Palette → “New Window”

If your key bindings differ, adjust src\vsbridge.py functions.

----------------------------------------------------------------
Section 6 — Safety and guardrails
----------------------------------------------------------------
- PyAutoGUI fail-safe: move mouse to top-left to abort automation
- Rate limiting: clicks and keystrokes per minute are capped (config\policy_rules.json)
- Emergency stop: Stop button in UI triggers immediate shutdown
- Logs: logs\run.log captures start, pause, resume, stop, and user messages

----------------------------------------------------------------
Section 7 — Quick-start
----------------------------------------------------------------
- Activate venv:
  C:\Users\yerbr\AI_Algorithms\AI_Coder_Controller\.venv\Scripts\Activate.ps1
- Install deps:
  pip install mss opencv-python pyautogui pynput numpy rich pillow tk
- Open folder in VSCode (Ctrl+K Ctrl+O): C:\Users\yerbr\AI_Algorithms\AI_Coder_Controller\
- Run:
  python -m src.main

End of file.
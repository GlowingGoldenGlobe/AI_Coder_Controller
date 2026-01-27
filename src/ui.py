import threading
import json
from pathlib import Path
from tkinter import Tk, Frame, Button, Listbox, Label, END, filedialog, StringVar, Spinbox, OptionMenu
import os
from rich.console import Console

console = Console()


class Hotkeys:
    def __init__(self, rules_path: Path):
        cfg = json.loads(Path(rules_path).read_text(encoding="utf-8"))
        self.state = {"recording": False, "paused": False, "stop": False}
        self.hotkeys = cfg.get("hotkeys", {})

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
    def __init__(self, root: Path, on_run, on_pause, on_resume, on_stop, on_user_msg, on_upload_files, on_select_project, on_focus_vscode, on_toggle_controls, on_list_windows=None, on_select_target=None):
        self.root_path = root
        self.on_run = on_run
        self.on_pause = on_pause
        self.on_resume = on_resume
        self.on_stop = on_stop
        self.on_user_msg = on_user_msg
        self.on_upload_files = on_upload_files
        self.on_select_project = on_select_project
        self.on_focus_vscode = on_focus_vscode
        self.on_toggle_controls = on_toggle_controls
        self.on_focus_terminal = None
        self.on_list_windows = on_list_windows
        self.on_select_target = on_select_target

        self.tk = Tk()
        self.tk.title("AI_Coder_Controller")
        self.status_var = StringVar(value="Idle")

        ctrl = Frame(self.tk)
        ctrl.pack(side="top", fill="x")
        Button(ctrl, text="Run", command=self.on_run).pack(side="left", padx=4, pady=4)
        Button(ctrl, text="Pause", command=self.on_pause).pack(side="left", padx=4, pady=4)
        Button(ctrl, text="Resume", command=self.on_resume).pack(side="left", padx=4, pady=4)
        Button(ctrl, text="Stop", command=self.on_stop).pack(side="left", padx=4, pady=4)
        self.controls_btn = Button(ctrl, text="Pause Controls", command=self._toggle_controls)
        self.controls_btn.pack(side="left", padx=8, pady=4)
        self.automation_btn = Button(ctrl, text="Automation: On", command=self._toggle_automation)
        self.automation_btn.pack(side="left", padx=8, pady=4)
        self.agent_btn = Button(ctrl, text="Agent Mode: Off", command=self._toggle_agent)
        self.agent_btn.pack(side="left", padx=8, pady=4)
        self.controls_timer_var = StringVar(value="")
        self.controls_timer_lbl = Label(ctrl, textvariable=self.controls_timer_var)
        self.controls_timer_lbl.pack(side="left", padx=4)

        side = Frame(self.tk)
        side.pack(side="left", fill="y")
        Button(side, text="Upload Files", command=self.upload_files).pack(pady=2)
        Button(side, text="Projects", command=self.list_projects).pack(pady=2)
        Button(side, text="Focus Terminal", command=self._focus_terminal).pack(pady=2)
        Button(side, text="Focus VS Code", command=self.on_focus_vscode).pack(pady=2)
        Button(side, text="Scroll Chat Down", command=self._scroll_chat_down).pack(pady=2)
        Button(side, text="Scroll Chat Up", command=self._scroll_chat_up).pack(pady=2)
        steps_frame = Frame(side)
        steps_frame.pack(pady=2)
        Label(steps_frame, text="Steps:").pack(side="left")
        self.scroll_steps_var = StringVar(value="3")
        self.scroll_steps_box = Spinbox(steps_frame, from_=1, to=20, width=4, textvariable=self.scroll_steps_var)
        self.scroll_steps_box.pack(side="left")
        Button(side, text="Open Recordings", command=self.open_recordings_folder).pack(pady=2)
        # OCR toggle and Planner test
        self.ocr_btn = Button(side, text="OCR: On", command=self._toggle_ocr)
        self.ocr_btn.pack(pady=2)
        Button(side, text="Test Planner", command=self._test_planner).pack(pady=2)
        Button(side, text="Send Metadata to Copilot", command=self._send_metadata).pack(pady=6)
        Button(side, text="Open Logs Folder", command=self.open_logs_folder).pack(pady=2)
        # Target App picker
        Label(side, text="Target App").pack(pady=(6,2))
        self.target_var = StringVar(value="(none)")
        self.target_menu = OptionMenu(side, self.target_var, "(none)", command=self._on_select_target)
        self.target_menu.pack(fill="x", padx=2)
        Button(side, text="Refresh Apps", command=self._refresh_windows).pack(pady=2)
        self.target_status_var = StringVar(value="Target: (none)")
        Label(side, textvariable=self.target_status_var).pack(pady=(2,0))
        self.foreground_var = StringVar(value="Foreground: (unknown)")
        Label(side, textvariable=self.foreground_var).pack(pady=(0,6))
        Label(side, text="Files").pack()
        self.files_list = Listbox(side, width=60)
        self.files_list.pack(fill="both", expand=True)

        main = Frame(self.tk)
        main.pack(side="right", fill="both", expand=True)
        Label(main, textvariable=self.status_var).pack(anchor="w")
        self.timer_label = Label(main, text="")
        self.timer_label.pack(anchor="w")
        Button(main, text="Send Message", command=self._send_msg).pack(pady=2)

    def upload_files(self):
        fps = filedialog.askopenfilenames(filetypes=[("Text/Markdown", "*.txt;*.md"), ("All", "*.*")])
        if not fps:
            return
        for p in fps:
            self.files_list.insert(END, p)
        self.on_upload_files([Path(p) for p in fps])

    def list_projects(self):
        proj_root = self.root_path / "projects"
        if not proj_root.exists():
            return
        for child in proj_root.iterdir():
            if child.is_dir():
                Button(self.tk, text=f"Project: {child.name}", command=lambda c=child: self._load_project(c)).pack(pady=1)

    def _load_project(self, proj_dir: Path):
        files = []
        for name in ("objectives.md", "instructions.md"):
            p = proj_dir / name
            if p.exists():
                files.append(p)
        self.files_list.delete(0, END)
        for p in files:
            self.files_list.insert(END, str(p))
        self.on_select_project(proj_dir, files)

    def _send_msg(self):
        msg_file = filedialog.askopenfilename(filetypes=[("Text/Markdown", "*.txt;*.md"), ("All", "*.*")])
        if not msg_file:
            return
        try:
            content = Path(msg_file).read_text(encoding="utf-8")
        except Exception:
            content = ""
        self.on_user_msg(content)

    def _send_metadata(self):
        try:
            handler = getattr(self, "on_send_metadata")
        except Exception:
            handler = None
        if callable(handler):
            try:
                handler()
            except Exception:
                console.log("Failed to send metadata to Copilot")

    def _get_scroll_steps(self) -> int:
        try:
            v = int(self.scroll_steps_var.get())
            if v < 1:
                v = 1
            if v > 20:
                v = 20
            return v
        except Exception:
            return 3

    def _scroll_chat_down(self):
        try:
            handler = getattr(self, "on_scroll_chat_down")
        except Exception:
            handler = None
        if callable(handler):
            try:
                steps = self._get_scroll_steps()
                handler(steps)
            except Exception:
                console.log("Failed to scroll chat down")

    def _scroll_chat_up(self):
        try:
            handler = getattr(self, "on_scroll_chat_up")
        except Exception:
            handler = None
        if callable(handler):
            try:
                steps = self._get_scroll_steps()
                handler(steps)
            except Exception:
                console.log("Failed to scroll chat up")

    def start(self):
        # Enforce Tk mainloop on the main thread (Windows tkinter requirement)
        if threading.current_thread() is not threading.main_thread():
            console.log("[red]Refusing to start Tk mainloop from a background thread[/red]")
            raise RuntimeError("Tk mainloop must run in the main thread on Windows")
        self.tk.mainloop()

    def set_controls_timer(self, text: str, color: str = "black"):
        try:
            self.timer_label.configure(text=text, fg=color)
        except Exception:
            pass

    def set_automation_state(self, enabled: bool):
        try:
            self.automation_btn.configure(text=("Automation: On" if enabled else "Automation: Off"))
        except Exception:
            pass

    def set_agent_state(self, enabled: bool):
        try:
            self.agent_btn.configure(text=("Agent Mode: On" if enabled else "Agent Mode: Off"))
        except Exception:
            pass

    def set_ocr_state(self, enabled: bool):
        try:
            self.ocr_btn.configure(text=("OCR: On" if enabled else "OCR: Off"))
        except Exception:
            pass

    def _toggle_automation(self):
        try:
            handler = getattr(self, "on_toggle_automation")
        except Exception:
            handler = None
        if callable(handler):
            try:
                new_state = bool(handler())
                self.set_automation_state(new_state)
            except Exception:
                console.log("Failed toggling automation state")

    def _toggle_agent(self):
        try:
            handler = getattr(self, "on_toggle_agent")
        except Exception:
            handler = None
        if callable(handler):
            try:
                new_state = bool(handler())
                self.set_agent_state(new_state)
            except Exception:
                console.log("Failed toggling agent mode")

    def _toggle_controls(self):
        try:
            paused = bool(self.on_toggle_controls())
            self.controls_btn.configure(text=("Resume Controls" if paused else "Pause Controls"))
            # Also surface a quick status hint
            self.status_var.set("Controls paused" if paused else "Controls active")
        except Exception:
            pass

    def _toggle_ocr(self):
        try:
            handler = getattr(self, "on_toggle_ocr")
        except Exception:
            handler = None
        if callable(handler):
            try:
                new_state = bool(handler())
                self.set_ocr_state(new_state)
            except Exception:
                console.log("Failed toggling OCR state")

    def _focus_terminal(self):
        try:
            handler = getattr(self, "on_focus_terminal")
        except Exception:
            handler = None
        if callable(handler):
            try:
                handler()
            except Exception:
                console.log("Failed to focus terminal")

    def _test_planner(self):
        try:
            handler = getattr(self, "on_test_planner")
        except Exception:
            handler = None
        if callable(handler):
            try:
                ok, msg = handler()
                self.status_var.set(msg if msg else ("Planner OK" if ok else "Planner failed"))
            except Exception:
                console.log("Planner test failed to run")

    def set_controls_timer(self, text: str, color: str | None = None):
        try:
            self.controls_timer_var.set(text)
            if color:
                self.controls_timer_lbl.configure(fg=color)
        except Exception:
            pass

    def open_recordings_folder(self):
        path = self.root_path / "recordings"
        try:
            os.startfile(str(path))
        except Exception:
            console.log(f"Failed to open {path}")

    def open_logs_folder(self):
        path = self.root_path / "logs"
        try:
            os.startfile(str(path))
        except Exception:
            console.log(f"Failed to open {path}")

    # Target App helpers
    def _refresh_windows(self):
        try:
            if callable(self.on_list_windows):
                items = list(self.on_list_windows())
            else:
                items = []
        except Exception:
            items = []
        if not items:
            items = ["(none)"]
        self.set_target_options(items)

    def set_target_options(self, options):
        try:
            menu = self.target_menu["menu"]
            menu.delete(0, "end")
            for opt in options:
                menu.add_command(label=opt, command=lambda v=opt: self.target_var.set(v))
            # ensure selection is valid
            cur = self.target_var.get()
            if cur not in options:
                self.target_var.set(options[0])
        except Exception:
            pass

    def _on_select_target(self, value):
        try:
            if callable(self.on_select_target):
                self.on_select_target(value)
            self.target_status_var.set(f"Target: {value}")
        except Exception:
            pass

    def set_foreground_status(self, text: str):
        try:
            self.foreground_var.set(text)
        except Exception:
            pass


def interactive_banner(console=console):
    console.rule("[bold cyan]AI_Coder_Controller[/]")
    console.print("UI provides Upload Files, Projects, sidebar listing, Run/Pause/Resume/Stop, and user message sending.", style="yellow")

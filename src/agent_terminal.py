from __future__ import annotations
from pathlib import Path
from typing import Optional, List
import shlex


class TerminalAgent:
    """Agent-side terminal runner that ALWAYS uses the VS Code integrated terminal.

    This is the single gateway for executing shell commands, launching the UI,
    or running Python modules. It delegates to VSBridge to focus the terminal
    and type commands, ensuring all executions happen inside VS Code.
    """

    def __init__(self, root: Path, vsbridge, action_log=None, python_exe: Optional[Path] = None):
        self.root = Path(root)
        self.vs = vsbridge
        self.log = action_log
        # Default to venv python within the project if not provided
        self.python_exe = Path(python_exe) if python_exe else (self.root / "Scripts" / "python.exe")
        self._pending_send: bool = False

    def _log(self, **kw):
        try:
            if self.log:
                self.log.log("agent_terminal", **kw)
        except Exception:
            pass

    def run_command(self, command: str) -> bool:
        """Run a single command string in the integrated terminal."""
        self._log(op="run_command", cmd_preview=command[:160])
        ok = self.vs.run_terminal_command(command)
        self._log(op="run_command", ok=ok)
        return bool(ok)

    def type_command_no_enter(self, command: str) -> bool:
        """Focus terminal and type command WITHOUT pressing Enter."""
        self._log(op="type_no_enter", cmd_preview=command[:160])
        try:
            focused = bool(self.vs.focus_terminal())
            if not focused:
                self._log(op="type_no_enter", ok=False, reason="focus_terminal_failed")
                return False
            if getattr(self.vs, "dry_run", False):
                # Log in dry-run, do not actually type
                self._log(op="type_no_enter", dry_run=True)
                return True
            # Best-effort foreground re-check before typing.
            try:
                verify = getattr(self.vs, "_verify_vscode_foreground", None)
                if callable(verify) and not bool(verify()):
                    self._log(op="type_no_enter", ok=False, reason="foreground_not_vscode")
                    return False
            except Exception:
                pass
            # Type characters without sending
            self.vs.ctrl.type_text(command)
            self._log(op="type_no_enter", ok=True)
            return True
        except Exception:
            self._log(op="type_no_enter", ok=False)
            return False

    def commit_enter(self) -> bool:
        """Press Enter in the terminal (sending any typed command)."""
        self._log(op="commit_enter")
        try:
            focused = bool(self.vs.focus_terminal())
            if not focused:
                self._log(op="commit_enter", ok=False, reason="focus_terminal_failed")
                return False
            if getattr(self.vs, "dry_run", False):
                self._log(op="commit_enter", dry_run=True)
                return True
            # Safety: ensure VS Code still foreground before sending Enter.
            try:
                verify = getattr(self.vs, "_verify_vscode_foreground", None)
                if callable(verify) and not bool(verify()):
                    self._log(op="commit_enter", ok=False, reason="foreground_not_vscode")
                    return False
            except Exception:
                pass
            self.vs.ctrl.press_keys(["enter"])
            self._log(op="commit_enter", ok=True)
            return True
        except Exception:
            self._log(op="commit_enter", ok=False)
            return False

    def queue_post_stop_send(self, command: str) -> bool:
        """Type a command now without Enter; mark for Enter on Stop."""
        ok = self.type_command_no_enter(command)
        if ok:
            self._pending_send = True
        return ok

    def has_pending(self) -> bool:
        return bool(self._pending_send)

    def commit_pending(self) -> bool:
        if not self._pending_send:
            return True
        ok = self.commit_enter()
        if ok:
            self._pending_send = False
        return ok

    def run_script(self, lines: List[str], joiner: str = " && ") -> bool:
        """Run multiple commands as a script.

        By default, joins with '&&' to stop on first failure.
        """
        cmd = joiner.join([ln.strip() for ln in lines if ln and ln.strip()])
        return self.run_command(cmd)

    def run_python_module(self, module: str, *args: str) -> bool:
        """Run a Python module via `-m` using the project venv python."""
        arg_str = " ".join(shlex.quote(a) for a in args)
        cmd = f"{self.python_exe} -m {module} {arg_str}".strip()
        return self.run_command(cmd)

    def launch_ui(self) -> bool:
        """Launch the AI_Coder_Controller UI inside the VS Code terminal."""
        return self.run_python_module("src.main")

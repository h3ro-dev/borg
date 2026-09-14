"""Minimal native macOS UI controls owned by BORG.

These calls use macOS's own ``open``, ``osascript`` and ``screencapture``
commands. No desktop MCP, browser MCP or nested agent is involved.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from fastmcp.exceptions import ToolError

from computer_tools import _path

TOOL_NAMES = ["ui_permissions", "ui_list_apps", "ui_launch", "ui_quit", "ui_capture", "ui_click", "ui_type"]


class NativeUI:
    def permissions(self) -> dict:
        return {"backend": "native_os", "screencapture": Path("/usr/sbin/screencapture").exists(),
                "osascript": Path("/usr/bin/osascript").exists(),
                "notice": "Accessibility and Screen Recording grants are reported only when a native action proves them."}

    def list_apps(self) -> dict:
        try:
            result = subprocess.run(["/bin/ps", "-axo", "comm="], capture_output=True, text=True, timeout=3, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ToolError("BORG native app list is unavailable") from None
        names = sorted({Path(line.strip()).name for line in result.stdout.splitlines() if line.strip()})
        return {"apps": names[:500]}

    def launch(self, name: str, foreground: bool = False) -> dict:
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ToolError("BORG app name is invalid")
        args = ["/usr/bin/open", "-a", name]
        try: result = subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ToolError("BORG native app launch is unavailable") from None
        if result.returncode != 0: raise ToolError("BORG native app launch failed")
        return {"name": name, "launched": True, "foreground_requested": bool(foreground)}

    def quit(self, name: str, force: bool = False) -> dict:
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ToolError("BORG app name is invalid")
        safe = name.replace('"', '\\"')
        script = f'tell application "{safe}" to quit'
        try: result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ToolError("BORG native app quit is unavailable") from None
        if result.returncode != 0 and not force: raise ToolError("BORG native app quit was refused")
        return {"name": name, "quit_requested": True, "force": bool(force), "returncode": result.returncode}

    def capture(self, path: str) -> dict:
        target = _path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try: result = subprocess.run(["/usr/sbin/screencapture", "-x", str(target)], capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ToolError("BORG native screen capture is unavailable") from None
        if result.returncode != 0 or not target.exists(): raise ToolError("BORG native screen capture was refused")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        return {"path": str(target), "bytes": target.stat().st_size, "sha256": digest}

    @staticmethod
    def _ui_script(app: str, body: str) -> str:
        if not isinstance(app, str) or not app.strip() or len(app) > 200:
            raise ToolError("BORG target app is invalid")
        safe = app.replace('"', '\\"')
        return f'tell application "System Events" to tell process "{safe}"\n{body}\nend tell'

    def click(self, app: str, x: int, y: int, foreground: bool = False) -> dict:
        if not foreground: raise ToolError("BORG UI click requires explicit foreground=true")
        if not (-20_000 <= int(x) <= 20_000 and -20_000 <= int(y) <= 20_000): raise ToolError("BORG UI coordinates are invalid")
        script = self._ui_script(app, f"click at {{{int(x)}, {int(y)}}}")
        result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=10, check=False)
        if result.returncode != 0: raise ToolError("BORG native UI click failed")
        return {"app": app, "clicked": True, "x": int(x), "y": int(y)}

    def type(self, app: str, text: str, foreground: bool = False) -> dict:
        if not foreground: raise ToolError("BORG UI typing requires explicit foreground=true")
        if not isinstance(text, str) or len(text) > 12_000: raise ToolError("BORG UI text is invalid or too long")
        escaped = text.replace('\\', '\\\\').replace('"', '\\"')
        script = self._ui_script(app, f'keystroke "{escaped}"')
        result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=10, check=False)
        if result.returncode != 0: raise ToolError("BORG native UI typing failed")
        return {"app": app, "typed": len(text)}


def mount_ui(server, config):
    ui = NativeUI()
    descriptions = {
        "ui_permissions": "Report native macOS UI command availability and permission proof boundaries.",
        "ui_list_apps": "List visible process applications from the native macOS process table.",
        "ui_launch": "Launch one named macOS application through the native open command.",
        "ui_quit": "Request one named macOS application to quit through native AppleScript.",
        "ui_capture": "Capture the native macOS screen to a local artifact path and return its checksum.",
        "ui_click": "Click native macOS coordinates in one named app; explicit foreground=true is required.",
        "ui_type": "Type text into one named macOS app; explicit foreground=true is required and text is never returned.",
    }
    annotations = {
        "ui_permissions": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "ui_list_apps": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "ui_launch": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
        "ui_quit": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
        "ui_capture": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
        "ui_click": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
        "ui_type": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    }
    methods = {name: name.removeprefix("ui_") for name in TOOL_NAMES}
    for name in TOOL_NAMES:
        server.tool(name=name, annotations=annotations[name], description=descriptions[name])(getattr(ui, methods[name]))

"""Start background helpers without a console, including Windows Node shims."""
import os
import shutil
import subprocess
from pathlib import Path


def hidden_process_options():
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startup}


def node_executable():
    command = shutil.which("node")
    if not command:
        raise FileNotFoundError("Node.js is not installed")
    # Version managers may put a console-launching shim on PATH. Start the
    # runtime itself so the worker has no intermediate process or extra window.
    result = subprocess.run(
        [command, "-p", "process.execPath"], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        encoding="utf-8", timeout=10, check=True, **hidden_process_options(),
    )
    executable = Path(result.stdout.strip())
    if not executable.is_absolute() or not executable.is_file():
        raise OSError("Node.js runtime is unavailable")
    return str(executable)

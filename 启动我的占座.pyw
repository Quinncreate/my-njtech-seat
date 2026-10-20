"""Double-click launcher: no terminal, no extra packages, reuse a healthy instance."""
import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from process_runtime import hidden_process_options

ROOT = Path(__file__).resolve().parent
URL = "http://127.0.0.1:5003"


def health():
    try:
        with urllib.request.urlopen(URL + "/api/health", timeout=1) as r:
            return json.load(r).get("app") == "my-njtech-seat"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def main():
    if not health():
        temp = ROOT.parents[1] / "tmp" / ROOT.name
        temp.mkdir(parents=True, exist_ok=True)
        executable = Path(sys.executable)
        if executable.name.lower() == "python.exe":
            windowless = executable.with_name("pythonw.exe")
            if windowless.is_file():
                executable = windowless
        env = os.environ.copy()
        env["PYTHONPYCACHEPREFIX"] = str(temp / "python-cache")
        # Server suppresses request logs; this file only receives startup failures.
        with (temp / "startup.log").open("ab") as log:
            proc = subprocess.Popen([str(executable), str(ROOT / "server.py")], cwd=ROOT,
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                                    **hidden_process_options())
        for _ in range(60):
            if health():
                break
            if proc.poll() is not None:
                raise RuntimeError("启动失败：端口 5003 可能被占用。请查看项目使用说明。")
            time.sleep(0.25)
        else:
            raise RuntimeError("程序启动较慢，请稍后再双击快捷方式。")
    webbrowser.open(URL + "/dashboard/tasks#new")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        ctypes.windll.user32.MessageBoxW(None, str(e), "我会去图书馆", 0x10)

from __future__ import annotations

import argparse
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from live_provider import BrowserWorker, LiveProvider
from seat_core import ACTIVE, Engine, Problem, Store

ROOT = Path(__file__).resolve().parent
TEMP = ROOT.parents[1] / "tmp" / ROOT.name


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5003)
    parser.add_argument("--data", type=Path, default=ROOT / "data" / "tasks.sqlite3")
    args = parser.parse_args()
    token, stopping, wake = secrets.token_urlsafe(32), threading.Event(), threading.Event()
    restore_requested = threading.Event()
    origin = f"http://127.0.0.1:{args.port}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # No URLs, library replies, cookies, tokens or personal records in logs.

        def respond(self, code, body, mime="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            for key, value in {
                "Content-Type": mime, "Content-Length": str(len(body)),
                "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            }.items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def valid_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{args.port}"

        def do_GET(self):
            if not self.valid_host():
                return self.respond(403, {"error": "请通过桌面快捷方式打开程序。"})
            url = urlsplit(self.path)
            try:
                if url.path == "/api/health":
                    return self.respond(200, {"app": "my-njtech-seat", "version": "0.5.1"})
                if url.path == "/api/state":
                    return self.respond(200, {**engine.view(), "csrf": token})
                if url.path == "/api/libraries":
                    return self.respond(200, engine.providers[store.data["mode"]].libraries())
                if url.path == "/api/seats":
                    lib = int(parse_qs(url.query).get("lib", [0])[0])
                    return self.respond(200, engine.providers[store.data["mode"]].seats(lib))
                if url.path == "/api/seatmap":
                    lib = int(parse_qs(url.query).get("lib", [0])[0])
                    return self.respond(200, engine.providers[store.data["mode"]].seatmap(lib))
                files = {"/": ("index.html", "text/html; charset=utf-8"),
                         "/dashboard/tasks": ("index.html", "text/html; charset=utf-8"),
                         "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                         "/style.css": ("style.css", "text/css; charset=utf-8"),
                         "/emblem.png": ("njtech-emblem.png", "image/png"),
                         "/favicon.ico": ("njtech-emblem.ico", "image/x-icon")}
                if url.path not in files:
                    return self.respond(404, {"error": "页面不存在。"})
                filename, mime = files[url.path]
                self.respond(200, (ROOT / "static" / filename).read_bytes(), mime)
            except Problem as e:
                self.respond(409, {"error": str(e)})
            except (ValueError, TypeError):
                self.respond(400, {"error": "请求内容不正确。"})
            except Exception:
                self.respond(500, {"error": "暂时无法读取，请稍后重试。"})

        def do_POST(self):
            if (not self.valid_host() or self.headers.get("Origin") != origin or
                    not secrets.compare_digest(self.headers.get("X-Seat-Token", ""), token)):
                return self.respond(403, {"error": "页面已过期，请刷新后重试。"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 0 or size > 16000:
                    return self.respond(413, {"error": "请求过大。"})
                body = json.loads(self.rfile.read(size) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError()
                url = urlsplit(self.path).path
                result = {"ok": True}
                if url == "/api/tasks":
                    result = engine.create(body)
                elif url.startswith("/api/tasks/"):
                    parts = url.split("/")
                    if len(parts) != 5:
                        raise Problem("操作地址不正确。")
                    if parts[4] == "edit":
                        engine.update(parts[3], body)
                    else:
                        if parts[4] == "finish" and body.get("release") is not True:
                            raise Problem("结束轮换现在会退座，请刷新页面并确认后再操作。")
                        result = engine.action(parts[3], parts[4], expected_owner=body.get("owner")) or result
                        if parts[4] in ("start", "resume", "finish", "handoff"):
                            wake.set()
                elif url == "/api/mode":
                    engine.switch(body.get("mode"))
                    if body.get("mode") == "live":
                        restore_requested.set()
                        wake.set()
                elif url == "/api/accounts/login":
                    if store.data["mode"] != "live":
                        raise Problem("请先切换为真实账号模式。")
                    result = engine.providers["live"].login(body.get("role"))
                    wake.set()
                elif url == "/api/accounts/switch":
                    with engine.lock:
                        if store.data["mode"] != "live":
                            raise Problem("请先切换为真实账号模式。")
                        if any(t["mode"] == "live" and (t["status"] in ACTIVE or t["status"] == "paused" or t.get("pending") or t.get("finish_requested")) for t in store.data["tasks"]):
                            raise Problem("请先结束当前任务，再切换账号。当前预约不会被主动退座。")
                        result = engine.providers["live"].switch_account(body.get("role"))
                        engine.accounts["live"][body.get("role")] = {"ready": False, "reservation": None}
                    wake.set()
                elif url == "/api/accounts/verify":
                    result = engine.refresh_accounts()
                elif url == "/api/demo/advance":
                    engine.advance_demo()
                elif url == "/api/shutdown":
                    stopping.set()
                    wake.set()
                    threading.Thread(target=server.shutdown, daemon=True).start()
                else:
                    return self.respond(404, {"error": "操作不存在。"})
                self.respond(200, result)
            except Problem as e:
                self.respond(409, {"error": str(e), "task_id": e.task_id})
            except (ValueError, TypeError, KeyError):
                self.respond(400, {"error": "请求内容不正确。"})
            except Exception:
                self.respond(500, {"error": "操作未确认，请刷新查看状态后再处理。"})

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    # Reserve the port before opening storage, so a duplicate launch cannot alter tasks.
    worker = BrowserWorker(TEMP / "browser")
    store = Store(args.data)
    engine = Engine(store, LiveProvider(worker))
    if store.data["mode"] == "live":
        restore_requested.set()

    def run():
        next_verification = 0
        while not stopping.is_set():
            try:
                if restore_requested.is_set():
                    restore_requested.clear()
                    engine.providers["live"].restore()
                    next_verification = 0
                live_idle = store.data["mode"] == "live" and not any(
                    t["mode"] == "live" and t["status"] in ACTIVE
                    for t in store.data["tasks"])
                if live_idle and time.monotonic() >= next_verification:
                    engine.refresh_accounts()
                    next_verification = time.monotonic() + 8
                engine.tick()
            except Exception:
                # Pending operation intents are retained on unexpected failures.
                pass
            wake.wait(4)
            wake.clear()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        stopping.set()
        wake.set()
        thread.join(timeout=40)
        worker.close()
        server.server_close()
        store.save()
        # A shutdown preserves tasks; next startup reconciles rather than replays.
        if not thread.is_alive():
            store.close()


if __name__ == "__main__":
    main()

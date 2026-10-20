"""Optional credentials encrypted for the current Windows user with DPAPI."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import threading
from pathlib import Path

from seat_core import Problem


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect(data: bytes, *, decrypt=False):
    if os.name != "nt":
        raise Problem("本机加密保存账号密码需要 Windows；仍可在学校页面直接登录。")
    source = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    # CRYPTPROTECT_UI_FORBIDDEN: never block the local server with a system prompt.
    if not operation(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise Problem("无法读取或保存本机加密凭据，请在当前 Windows 用户下重新填写。")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel.LocalFree(ctypes.cast(outgoing.data, ctypes.c_void_p))


class CredentialStore:
    def __init__(self, path=None):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        self.path = Path(path) if path else base / "MyNjtechSeat" / "credentials.dpapi"
        self.lock = threading.Lock()

    @staticmethod
    def role(value):
        if value not in ("A", "B"):
            raise Problem("账号标记无效。")
        return value

    def _read(self):
        if not self.path.exists():
            return {}
        try:
            data = json.loads(protect(self.path.read_bytes(), decrypt=True))
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("accounts"), dict):
                raise ValueError()
            accounts = data["accounts"]
            for role, item in accounts.items():
                if role not in ("A", "B") or not isinstance(item, dict):
                    raise ValueError()
                if not all(isinstance(item.get(key), str) for key in ("username", "password")):
                    raise ValueError()
            return accounts
        except (OSError, ValueError, TypeError):
            raise Problem("本机凭据文件不可读取，未修改原文件。") from None

    def get(self, role):
        self.role(role)
        with self.lock:
            return self._read().get(role, {"username": "", "password": ""}).copy()

    def save(self, role, username, password):
        self.role(role)
        if not isinstance(username, str) or not isinstance(password, str):
            raise Problem("请填写账号和密码。")
        username = username.strip()
        if not username or not password or len(username) > 128 or len(password) > 512:
            raise Problem("请填写有效的账号和密码；保留原绑定需填写原账号。")
        with self.lock:
            accounts = self._read()
            accounts[role] = {"username": username, "password": password}
            encrypted = protect(json.dumps({"version": 1, "accounts": accounts}, ensure_ascii=False).encode("utf-8"))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Only encrypted bytes touch the filesystem, including the temp file.
            temporary = self.path.with_suffix(".dpapi.tmp")
            with temporary.open("wb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                temporary.replace(self.path)
            except OSError as exc:
                if exc.errno != 18:  # Some Windows filesystem overlays reject rename.
                    raise
                self.path.write_bytes(encrypted)
        return {"saved": True}

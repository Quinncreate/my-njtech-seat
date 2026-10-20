"""Own GraphQL adapter, using official pages in isolated browser contexts.

Login is performed on the school's website in separate local Chrome profiles.
Passwords are not saved. Request headers stay in memory; Chrome retains cookies.
"""
from __future__ import annotations

import datetime as dt
import json
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from seat_core import Problem
from seat_layout import jiangpu_libraries, normalize_layout

INDEX = "query index { userAuth { reserve { reserve { lib_id seat_key exp_date exp_date_str status } getSToken } } }"
LIBS = "query list { userAuth { reserve { libs(libType:-1) { lib_id lib_name is_open lib_floor lib_group_id } libGroups { id group_name } } } }"
LAYOUT = """query libLayout($libId:Int,$libType:Int) { userAuth { reserve {
  libs(libType:$libType,libId:$libId) { lib_id lib_name is_open lib_floor lib_layout {
    max_x max_y
    seats { key name type seat_status status x y }
  } }
} } }"""
RESERVE = """mutation reserueSeat($libId:Int!,$seatKey:String!,$captchaCode:String,$captcha:String!) {
 userAuth { reserve { reserueSeat(libId:$libId,seatKey:$seatKey,captchaCode:$captchaCode,captcha:$captcha) } }
}"""
CANCEL = "mutation reserveCancle($sToken:String!) { userAuth { reserve { reserveCancle(sToken:$sToken) { timerange img hours mins per } } } }"


def graphql_payload(document, variables=None):
    payload = {"query": document, "variables": variables or {}}
    operation = re.match(r"\s*(?:query|mutation)\s+([_A-Za-z][_0-9A-Za-z]*)\b", document)
    if operation:
        payload["operationName"] = operation[1]
    return payload


def expiry(value, fallback=None):
    for item in (value, fallback):
        if item in (None, ""):
            continue
        try:
            n = float(item)
            if n > 1e12:
                n /= 1000
            if 946684800 < n < 4102444800:
                return n
        except (ValueError, TypeError):
            pass
        try:
            parsed = dt.datetime.fromisoformat(str(item).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
            return parsed.timestamp()
        except ValueError:
            pass
    raise Problem("学校未返回可识别的预约到期时间，请核对学校页面。")


class BrowserWorker:
    def __init__(self, temporary: Path):
        self.temporary, self.proc = temporary, None
        self.lock = threading.Lock()
        self.messages = queue.Queue()
        self.seq = 0

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        node = shutil.which("node")
        if not node:
            raise Problem("找不到浏览器组件 Node.js，请安装 Node.js 22 或更高版本。")
        self.temporary.mkdir(parents=True, exist_ok=True)
        self.messages = queue.Queue()
        self.proc = subprocess.Popen(
            [node, str(Path(__file__).with_name("browser_worker.js")), str(self.temporary)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        messages, proc = self.messages, self.proc

        def read():
            try:
                for line in proc.stdout:
                    try:
                        messages.put(json.loads(line))
                    except ValueError:
                        continue
            finally:
                messages.put({"closed": True})
        threading.Thread(target=read, daemon=True).start()

    def call(self, method, **params):
        with self.lock:
            self.start()
            self.seq += 1
            id = self.seq
            try:
                self.proc.stdin.write(json.dumps({"id": id, "method": method, **params}, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
                while True:
                    response = self.messages.get(timeout=90 if method == "restore" else 35)
                    if response.get("closed"):
                        raise Problem("登录浏览器已关闭，请重新登录并验证。")
                    if response.get("id") != id:
                        continue
                    if response.get("error"):
                        if response.get("code") == "IDENTITY_MISMATCH":
                            raise Problem("登录账号与固定绑定不符，请重新登录原账号；更换账号请使用“切换账号”。")
                        raise Problem("浏览器连接不可用，请打开学校选座页面后重新验证登录。")
                    return response.get("result")
            except (queue.Empty, OSError, BrokenPipeError):
                raise Problem("学校页面响应超时，请核对实际预约后恢复。") from None

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.terminate()


class LiveProvider:
    def __init__(self, worker):
        self.worker = worker
        self.tokens = {}
        self.catalog = None
        self.catalog_at = 0
        self.account_info = {}

    def restore(self):
        self.account_info = self.worker.call("restore")

    def switch_account(self, role):
        if role not in ("A", "B"):
            raise Problem("账号标记无效。")
        self.tokens.pop(role, None)
        self.catalog = None
        info = self.worker.call("switch", role=role)
        self.account_info[role] = info
        return info

    def login(self, role):
        if role not in ("A", "B"):
            raise Problem("账号标记无效。")
        return self.worker.call("login", role=role)

    def query(self, role, document, variables=None):
        result = self.worker.call("fetch", role=role, payload=graphql_payload(document, variables))
        return self.unpack(result)

    @staticmethod
    def unpack(result):
        if not isinstance(result, dict) or result.get("errors") or not isinstance(result.get("data"), dict):
            raise Problem("学校接口未确认成功，可能需要重新登录或在学校页面处理验证码。")
        auth = result["data"].get("userAuth")
        if not isinstance(auth, dict) or not isinstance(auth.get("reserve"), dict):
            raise Problem("登录状态已失效，请重新验证。")
        return auth["reserve"]

    def observe(self):
        result = {}
        for role in ("A", "B"):
            try:
                data = self.query(role, INDEX)
                reservation = data.get("reserve")
                self.tokens[role] = data.get("getSToken")
                if not self.tokens[role]:
                    raise Problem("学校未提供有效的登录校验信息，请重新登录。")
                if reservation and reservation.get("seat_key"):
                    if not reservation.get("lib_id"):
                        raise Problem("预约记录缺少馆室，请核对学校页面。")
                    reservation = {"lib": int(reservation["lib_id"]), "seat": str(reservation["seat_key"]),
                                   "expires": expiry(reservation.get("exp_date"), reservation.get("exp_date_str"))}
                else:
                    reservation = None
                info = self.worker.call("confirmIdentity", role=role)
                if isinstance(info, dict):
                    self.account_info[role] = info
                result[role] = {"ready": True, "reservation": reservation,
                                "label": self.account_info.get(role, {}).get("label", "固定账号 " + role)}
            except Problem as exc:
                self.tokens.pop(role, None)
                result[role] = {"ready": False, "reservation": None,
                                "label": self.account_info.get(role, {}).get("label", "固定账号 " + role),
                                "reason": str(exc)}
        return result

    def read_role(self):
        # A failed account can be reconnected without copying sessions between accounts.
        for role in ("A", "B"):
            try:
                return role, self.query(role, LIBS)
            except Problem:
                continue
        raise Problem("请先登录一个账号、进入学校选座页面，再点击验证登录。")

    def libraries(self):
        if self.catalog is None or time.monotonic() - self.catalog_at > 30:
            _, data = self.read_role()
            self.catalog = jiangpu_libraries(data)
            self.catalog_at = time.monotonic()
        return self.catalog

    def seats(self, lib):
        return self.seatmap(lib)["seats"]

    def seatmap(self, lib):
        info = next((r for r in self.libraries() if r["id"] == lib), None)
        if not info:
            raise Problem("请选择江浦校区逸夫或浦江图书馆的馆室。")
        role, _ = self.read_role()
        data = self.query(role, LAYOUT, {"libId": lib, "libType": -1})
        rooms = data.get("libs") or []
        if isinstance(rooms, dict):
            rooms = [rooms]
        room = next((x for x in rooms if int(x.get("lib_id", -1)) == lib), None)
        if not room:
            raise Problem("馆室信息已变化，请重新选择。")
        return normalize_layout(room, info)

    def reserve(self, role, task):
        variables = {"libId": task["lib"], "seatKey": task["seat"], "captchaCode": "", "captcha": ""}
        # Match the mutation used by the school's current seat-map page.
        field = "reserueSeat"
        result = self.worker.call("fetch", role=role, payload=graphql_payload(RESERVE, variables))
        errors = result.get("errors") if isinstance(result, dict) else None
        # Validation errors happen before GraphQL executes a mutation. No retries for
        # transport errors, business errors or an unknown mutation outcome.
        if (errors and not result.get("data") and all(
                isinstance(e, dict) and str(e.get("message", "")).startswith('Cannot query field "reserueSeat"')
                for e in errors)):
            field = "reserveSeat"
            result = self.worker.call("fetch", role=role,
                                      payload=graphql_payload(RESERVE.replace("reserueSeat", field), variables))
        data = self.unpack(result)
        if not data.get(field):
            raise Problem("学校没有确认选座成功，请在学校页面核对。")

    def cancel(self, role):
        token = self.tokens.get(role)
        if not token:
            raise Problem("退座校验已失效，已保留当前预约。")
        try:
            self.query(role, CANCEL, {"sToken": token})
        except Problem:
            # The school can release the seat and then fail while building its
            # response. Never repeat the mutation: read the actual reservation.
            pass
        try:
            actual = self.query(role, INDEX)
        except Problem:
            raise Problem("退座结果暂时无法核对，已停止接棒，请在学校页面确认。") from None
        if "reserve" not in actual or not actual.get("getSToken"):
            raise Problem("退座结果缺少有效的账号核对信息，已停止接棒。")
        self.tokens[role] = actual["getSToken"]
        if actual["reserve"] is not None:
            raise Problem("学校仍有预约记录，已停止接棒，请在学校页面核对。")

"""Task persistence and a reservation state machine. No credentials are stored here."""
from __future__ import annotations

import copy
import json
import math
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from seat_layout import CAMPUS

COOLDOWN = 65
ACTIVE = {"running", "recovering", "scheduled", "switching", "finishing"}


class Problem(Exception):
    def __init__(self, message, *, task_id=None):
        super().__init__(message)
        self.task_id = task_id


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        row = self.db.execute("SELECT body FROM state WHERE id=1").fetchone()
        self.data = json.loads(row[0]) if row else {
            "mode": "demo", "offset": 0, "tasks": [], "events": [],
            "cooldowns": {}, "demo_seats": {},
        }
        self.save()

    def save(self):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES (1, ?)",
                            (json.dumps(self.data, ensure_ascii=False),))
            self.committed = copy.deepcopy(self.data)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.committed)

    def close(self):
        self.db.close()


class DemoProvider:
    def __init__(self, store, now):
        self.store, self.now = store, now

    def observe(self):
        seats = self.store.data["demo_seats"]
        for role in list(seats):
            if seats[role]["expires"] <= self.now():
                del seats[role]
        return {r: {"ready": True, "reservation": copy.deepcopy(seats.get(r))} for r in ("A", "B")}

    def reserve(self, role, task):
        states = self.observe()
        if states[role]["reservation"] or any(
            x["reservation"] and x["reservation"]["seat"] == task["seat"]
            and x["reservation"]["lib"] == task["lib"] for x in states.values()
        ):
            raise Problem("座位或账号已有预约，请先核对。")
        if self.store.data["cooldowns"].get("demo:" + role, 0) > self.now():
            raise Problem("账号仍在退座冷却中。")
        self.store.data["demo_seats"][role] = {
            "lib": task["lib"], "seat": task["seat"], "expires": self.now() + 1800,
        }

    def cancel(self, role):
        self.store.data["demo_seats"].pop(role, None)

    def libraries(self):
        return [{"id": i, "name": name, "campus": CAMPUS, "building": building,
                 "floor": "1楼", "is_open": True} for i, name, building in (
                    (1, "模拟阅览室 A", "逸夫图书馆"), (2, "模拟阅览室 B", "浦江图书馆"))]

    def seats(self, lib):
        return self.seatmap(lib)["seats"]

    def seatmap(self, lib):
        info = next((r for r in self.libraries() if r["id"] == lib), None)
        if not info:
            raise Problem("请选择江浦校区的馆室。")
        held = {r["seat"] for r in self.store.snapshot()["demo_seats"].values()
                if r["lib"] == lib and r["expires"] > self.now()}
        seats = []
        for i in range(1, 49):
            key = f"{lib}-{i:03d}"
            state = "occupied" if key in held or i % 7 == 0 else "unknown" if i % 11 == 0 else "available"
            col, row = (i - 1) % 8, (i - 1) // 8
            seats.append({"key": key, "name": f"{i:03d}", "x": 2 + col * 3 + (3 if col >= 4 else 0),
                          "y": 2 + row * 3 + (2 if row >= 3 else 0), "state": state,
                          "available": state == "available", "status": {
                              "available": "空闲", "occupied": "已占用", "unknown": "状态未知"}[state]})
        return {**info, "lib": lib, "source": "demo", "observed_at": time.time(),
                "max_x": 29, "max_y": 23, "seats": seats, "total_count": len(seats),
                "available_count": sum(s["available"] for s in seats)}


class Engine:
    def __init__(self, store, live=None, clock=time.time):
        self.store, self.clock = store, clock
        self.lock = threading.RLock()
        self.providers = {"demo": DemoProvider(store, lambda: self.now("demo"))}
        if live:
            self.providers["live"] = live
        self.accounts = {m: {r: {"ready": m == "demo", "reservation": None} for r in ("A", "B")}
                         for m in ("demo", "live")}
        for t in store.data["tasks"]:
            if t.get("finish_requested") and t["status"] != "completed":
                t["status"], t["message"] = "finishing", "正在核对结束任务的退座结果。"
            elif t["status"] in ACTIVE and t["status"] != "scheduled":
                t["status"], t["message"] = "recovering", "正在核对两个账号的实际预约。"
        store.save()

    def now(self, mode=None):
        return self.clock() + (self.store.data["offset"] if (mode or self.store.data["mode"]) == "demo" else 0)

    def event(self, message, task=None):
        self.store.data["events"].insert(0, {
            "time": self.clock(), "message": message, "task": task and task["id"],
        })
        del self.store.data["events"][200:]

    def get_task(self, id):
        return next((t for t in self.store.data["tasks"] if t["id"] == id), None)

    def view(self):
        data = self.store.snapshot()
        mode = data["mode"]
        accounts = copy.deepcopy(self.accounts[mode])
        now = self.now(mode)
        for r, a in accounts.items():
            a["cooldown"] = max(0, math.ceil(data["cooldowns"].get(mode + ":" + r, 0) - now))
        return {"mode": mode, "now": now, "accounts": accounts,
                "tasks": data["tasks"], "events": data["events"], "version": "0.5.1"}

    def create(self, body, *, _original=None):
        with self.lock:
            mode = self.store.data["mode"]
            task = self.validate(body, mode, _original)
            task.update(id=uuid.uuid4().hex[:12], mode=mode, status="draft", owner=None,
                        acquired=0, next=0, expires=0, rotations=0, pending=None,
                        message="准备好后点击开始。", created=self.clock())
            self.store.data["tasks"].insert(0, task)
            self.event("已创建任务", task)
            self.store.save()
            return copy.deepcopy(task)

    def validate(self, body, mode, original=None):
        try:
            name, seat = str(body.get("name", "")).strip(), str(body.get("seat", "")).strip()
            lib, lead = int(body["lib"]), int(body.get("lead", 300))
            start, end = float(body.get("start") or 0), float(body.get("end") or 0)
            if not name or len(name) > 60 or not seat or len(seat) > 100 or lib <= 0:
                raise ValueError()
            if lead < 30 or lead > 900 or not all(math.isfinite(x) for x in (start, end)):
                raise ValueError()
            if end and end <= max(start, self.now(mode)):
                raise Problem("结束时间需要晚于开始时间和当前时间。")
        except (ValueError, TypeError, KeyError):
            raise Problem("请填写任务名称、馆室和座位；提前轮换时间应为 30 至 900 秒。") from None
        # Validate the association against the current provider, not a stale form's library.
        options = self.providers[mode].seats(lib)
        selected = next((s for s in options if s["key"] == seat), None)
        if not selected:
            raise Problem("座位不属于当前馆室，请重新选择。")
        unchanged = original and (lib, seat) == (original["lib"], original["seat"])
        if selected.get("available") is not True and not unchanged:
            raise Problem("这个座位已占用或状态不明，请刷新后选择空闲座位。")
        return {"name": name, "lib": lib, "seat": seat, "seat_name": selected["name"],
                "lib_name": str(body.get("lib_name", ""))[:100], "lead": lead, "start": start, "end": end}

    def update(self, id, body):
        with self.lock:
            t = self.get_task(id)
            if not t or t["status"] not in ("draft", "paused", "attention"):
                raise Problem("请先暂停任务，再编辑。")
            if t.get("finish_requested"):
                raise Problem("任务正在等待退座确认，请先完成结束退座。")
            if t["pending"]:
                raise Problem("上次操作结果待核对，请先恢复任务。")
            fields = self.validate(body, t["mode"], t)
            if t["owner"] and (fields["lib"], fields["seat"]) != (t["lib"], t["seat"]):
                raise Problem("当前已有持座；请结束轮换并另外创建新座位任务。")
            t.update(fields)
            self.event("已编辑任务", t)
            self.store.save()

    def action(self, id, action, *, expected_owner=None):
        with self.lock:
            t = self.get_task(id)
            if not t:
                raise Problem("任务不存在。")
            if action == "rerun":
                if t["status"] in ACTIVE or t["status"] == "paused" or t.get("finish_requested") or t["pending"]:
                    raise Problem("请先结束原任务。")
                body = {k: t[k] for k in ("name", "lib", "seat", "lib_name", "lead")}
                body["name"] = body["name"][:55] + " · 重跑"
                if t["mode"] != self.store.data["mode"]:
                    raise Problem("请先切换到原任务的运行模式。")
                return self.create(body, _original=t)
            if action in ("start", "resume"):
                if t.get("finish_requested"):
                    raise Problem("任务已要求结束，请先完成退座，不能恢复轮换。")
                if t["status"] not in ("draft", "paused", "attention"):
                    raise Problem("任务当前不能启动。")
                if t["end"] and t["end"] <= self.now(t["mode"]):
                    raise Problem("结束时间已过，请先编辑。")
                self.prepare_start(t)
                t["status"], t["message"] = "recovering", "正在核对预约和账号冷却。"
                if t["start"] > self.now(t["mode"]):
                    t["status"], t["message"] = "scheduled", "等待计划开始时间。"
            elif action == "pause":
                if t["status"] not in ("running", "recovering", "scheduled") or t.get("finish_requested") or t.get("manual_handoff"):
                    raise Problem("任务当前没有运行。")
                t["status"], t["message"] = "paused", "已暂停轮换，现有预约保留至学校规定的到期时间。"
            elif action == "finish":
                if t["status"] == "completed":
                    raise Problem("任务已结束。")
                if t["status"] == "draft" and not t["owner"] and not t["pending"]:
                    t["status"], t["message"] = "completed", "草稿已结束，未操作预约。"
                else:
                    self.exclusive(t)
                    # Only an explicit retry may authorize a second finish request.
                    if t.get("finish_requested") and t["status"] == "attention":
                        t["pending"] = None
                    t["finish_requested"], t["manual_handoff"] = True, None
                    t["status"], t["message"] = "finishing", "已停止轮换，正在核对并释放本任务座位。"
            elif action == "handoff":
                self.exclusive(t)
                if (t["status"] not in ("running", "paused") or t.get("finish_requested") or
                        t["pending"] or t.get("manual_handoff") or t["owner"] not in ("A", "B")):
                    raise Problem("请在账号实际持座且没有待核对操作时切换。")
                if expected_owner != t["owner"]:
                    raise Problem("持座账号已变化，请刷新后再切换。")
                if t["end"] and t["end"] <= self.now(t["mode"]):
                    raise Problem("任务时间已结束，不能切换持座账号。")
                other = "B" if t["owner"] == "A" else "A"
                remaining = math.ceil(max(self.ready_at(t["mode"], other), t["acquired"] + COOLDOWN) - self.now(t["mode"]))
                if remaining > 0:
                    raise Problem(f"暂不能切换，还需保护等待 {remaining} 秒；当前预约已保留。")
                t["manual_handoff"] = {"from": t["owner"], "paused": t["status"] == "paused"}
                t["status"], t["message"] = "switching", f"正在核对账号 {other}，准备接棒同一座位。"
            else:
                raise Problem("不支持的操作。")
            self.event(t["message"], t)
            self.store.save()

    def switch(self, mode):
        with self.lock:
            if mode not in self.providers:
                raise Problem("运行模式不可用。")
            if any(t["status"] in ACTIVE or t["status"] == "paused" or t["pending"] or t.get("finish_requested") for t in self.store.data["tasks"]):
                raise Problem("请先结束当前任务，再切换运行模式。")
            self.store.data["mode"] = mode
            self.event("已切换为" + ("模拟演示" if mode == "demo" else "真实账号模式"))
            self.store.save()

    def ready_at(self, mode, role):
        return self.store.data["cooldowns"].get(mode + ":" + role, 0)

    def blocking_tasks(self, t):
        return [x for x in self.store.data["tasks"]
                if x["id"] != t["id"] and x["mode"] == t["mode"] and
                (x["status"] in ACTIVE or x["status"] == "paused" or x["pending"] or
                 x.get("finish_requested") or x.get("manual_handoff"))]

    @staticmethod
    def task_conflict(t, detail="请先结束该任务，再开始新任务。"):
        status = {"paused": "已暂停", "scheduled": "等待开始", "running": "运行中",
                  "recovering": "正在核对", "switching": "正在切换", "finishing": "正在退座",
                  "attention": "需要处理"}.get(t["status"], "待核对")
        return Problem(f'任务“{t["name"]}”（{status}，座位 {t["seat_name"]}，编号 {t["id"][:6]}）'
                       f'尚未结束。{detail}', task_id=t["id"])

    def prepare_start(self, t):
        blockers = self.blocking_tasks(t)
        if not blockers:
            return
        # Only dormant, confirmed empty pauses can be retired. Pending operations
        # and running/scheduled tasks keep their claim even when accounts look free.
        uncertain = next((x for x in blockers if x["status"] != "paused" or x["pending"] or
                          x.get("finish_requested") or x.get("manual_handoff")), None)
        if uncertain:
            raise self.task_conflict(uncertain)
        try:
            states = self.observe(t["mode"])
        except Exception:
            self.store.save()
            raise self.task_conflict(blockers[0], "尚未确认两个账号均已空闲，请先验证账号并核对旧任务。") from None
        if any(a["reservation"] for a in states.values()):
            self.store.save()
            raise self.task_conflict(blockers[0], "账号仍有预约，请先核对或结束该任务。")
        for old in blockers:
            old.update(status="completed", owner=None, next=0, expires=0,
                       message="启动新任务前已核实两个账号均无预约，旧的暂停任务已结束。")
            self.event(old["message"], old)

    def exclusive(self, t):
        blockers = self.blocking_tasks(t)
        if blockers:
            raise self.task_conflict(blockers[0], "请先处理该任务，再操作当前任务。")

    def attention(self, t, message):
        if t["status"] != "attention" or t["message"] != message:
            self.event(message, t)
        t["status"], t["message"] = "attention", message
        t["manual_handoff"] = None

    def observe(self, mode):
        states = self.providers[mode].observe()
        self.record_observation(mode, states)
        if not all(states.get(r, {}).get("ready") for r in ("A", "B")):
            raise Problem("请登录并验证两个账号，任务会在登录恢复后重新核对。")
        return states

    def record_observation(self, mode, states):
        previous = self.accounts[mode]
        for role in ("A", "B"):
            new, old = states[role], previous[role]
            lost = old["reservation"] and new["ready"] and not new["reservation"]
            reconnected_free = mode == "live" and not old["ready"] and new["ready"] and not new["reservation"]
            if lost or reconnected_free:
                key = mode + ":" + role
                self.store.data["cooldowns"][key] = max(self.ready_at(mode, role), self.now(mode) + COOLDOWN)
        self.accounts[mode] = states

    def refresh_accounts(self):
        with self.lock:
            mode = self.store.data["mode"]
            self.record_observation(mode, self.providers[mode].observe())
            self.store.save()
            return self.view()

    def tick(self):
        with self.lock:
            tasks = [t for t in self.store.data["tasks"] if t["status"] in ACTIVE]
            for t in tasks:
                now = self.now(t["mode"])
                if t.get("finish_requested"):
                    try:
                        self.finish_step(t)
                    except Exception:
                        self.attention(t, "已停止轮换，但退座尚未确认；请恢复登录后点击“重试退座”。")
                    continue
                if t["end"] and now >= t["end"]:
                    if t["pending"]:
                        self.attention(t, "已到结束时间，但上次操作结果待核对。请验证账号后点击结束轮换。")
                        continue
                    t["status"], t["message"] = "completed", "已到结束时间，停止轮换并保留当前预约。"
                    self.event(t["message"], t)
                    continue
                if t["start"] > now:
                    t["status"] = "scheduled"
                    continue
                try:
                    states = self.observe(t["mode"])
                    if t.get("manual_handoff") and not t["pending"]:
                        self.manual_step(t, states)
                    else:
                        self.step(t, states)
                except Problem as e:
                    t["status"], t["message"] = "recovering", str(e)
                except Exception:
                    t["status"], t["message"] = "recovering", "连接中断，正在等待重新核对；不会盲目重复操作。"
            self.store.save()

    @staticmethod
    def matches(t, reservation):
        return reservation and reservation["lib"] == t["lib"] and reservation["seat"] == t["seat"]

    def finish_step(self, t):
        mode, p = t["mode"], self.providers[t["mode"]]
        states = p.observe()
        self.record_observation(mode, states)
        holders = [r for r in ("A", "B") if states[r]["ready"] and self.matches(t, states[r]["reservation"])]
        if len(holders) > 1:
            return self.attention(t, "两个账号的预约记录冲突，无法确认退座，请在学校页面核对。")
        if holders:
            role = holders[0]
            t["owner"], t["expires"] = role, states[role]["reservation"]["expires"]
            if t["pending"] and t["pending"]["kind"] == "finish_cancel":
                return self.attention(t, "学校仍显示本任务座位，已停止轮换；核对后可点击“重试退座”。")
            t["pending"] = {"kind": "finish_cancel", "role": role, "time": self.now(mode)}
            self.store.save()
            try:
                p.cancel(role)
            except Problem:
                pass  # A lost reply is resolved by reading, never by an automatic retry.
            states = p.observe()
            self.record_observation(mode, states)
            if any(self.matches(t, s["reservation"]) for s in states.values()):
                return self.attention(t, "退座尚未确认，已停止轮换；请核对学校页面后点击“重试退座”。")
        if not all(states[r]["ready"] for r in ("A", "B")):
            return self.attention(t, "已停止轮换，尚有账号登录不可用，无法完整确认退座；恢复登录后点击“重试退座”。")
        pending = t["pending"]
        if pending and pending["kind"] in ("cancel", "finish_cancel"):
            role = pending["role"]
            self.store.data["cooldowns"][mode + ":" + role] = max(self.ready_at(mode, role), self.now(mode) + COOLDOWN)
        t.update(status="completed", message="轮换已结束，已确认本任务座位不再由这两个账号占用。",
                 owner=None, pending=None, next=0, expires=0, finish_requested=False, manual_handoff=None)
        self.event(t["message"], t)

    def manual_step(self, t, states):
        request = t["manual_handoff"]
        role, mode = request["from"], t["mode"]
        other = "B" if role == "A" else "A"
        if not self.matches(t, states[role]["reservation"]) or states[other]["reservation"]:
            return self.attention(t, "持座情况已变化或接棒账号已有预约，未执行切换，请核对后恢复。")
        remaining = math.ceil(max(self.ready_at(mode, other), t["acquired"] + COOLDOWN) - self.now(mode))
        if remaining > 0:
            return self.attention(t, f"暂不能切换，还需保护等待 {remaining} 秒；当前预约已保留。")
        t["pending"] = {"kind": "cancel", "role": role, "time": self.now(mode)}
        self.store.save()
        self.providers[mode].cancel(role)
        self.store.data["cooldowns"][mode + ":" + role] = self.now(mode) + COOLDOWN
        self.store.save()
        self.acquire(t, other, True)

    def step(self, t, states):
        now, mode = self.now(t["mode"]), t["mode"]
        p = self.providers[mode]
        holders = [r for r in ("A", "B") if self.matches(t, states[r]["reservation"])]
        if any(s["reservation"] and not self.matches(t, s["reservation"]) for s in states.values()):
            return self.attention(t, "账号已有其他座位的预约，请在学校页面核对后恢复任务。")
        if len(holders) > 1:
            return self.attention(t, "两个账号的预约记录冲突，请人工核对。")
        if not holders:
            if t["pending"]:
                pending = t["pending"]
                if pending.get("kind") == "cancel":
                    # If a crash followed a release, assume a fresh cooldown conservatively.
                    role = pending["role"]
                    self.store.data["cooldowns"][mode + ":" + role] = max(self.ready_at(mode, role), now + COOLDOWN)
                t["pending"] = None
                t["owner"], t["next"], t["expires"] = None, 0, 0
                return self.attention(t, "上次操作未确认且当前没有持座，请核对学校页面后点击恢复。")
            if t["owner"]:
                t["owner"], t["next"], t["expires"] = None, 0, 0
                return self.attention(t, "原预约已结束或被外部操作改变，请核对后恢复任务。")
            choices = [r for r in ("A", "B") if self.ready_at(mode, r) <= now]
            if not choices:
                t["next"] = min(self.ready_at(mode, r) for r in ("A", "B"))
                remaining = max(0, math.ceil(t["next"] - now))
                t["status"], t["message"] = "running", f"账号冷却中，约 {remaining} 秒后开始选座。"
                return
            return self.acquire(t, choices[0], False)
        role = holders[0]
        reservation = states[role]["reservation"]
        pending = t["pending"]
        manual = t.get("manual_handoff")
        if manual and role == manual["from"] and pending and pending["kind"] == "reserve":
            return self.attention(t, "接棒账号尚未取得座位，已停止切换，请核对实际预约。")
        if pending and pending["kind"] == "cancel":
            t["pending"] = None
            return self.attention(t, "上次退座请求结果不确定，已停止继续退座，请核对学校页面后恢复。")
        if pending and pending["kind"] == "reserve" and pending.get("handoff") and t["owner"] != role:
            t["rotations"] += 1
            self.event("轮换完成：账号 " + role + " 已接棒同一座位", t)
        if t["owner"] != role:
            t["acquired"] = now
        t["owner"], t["expires"], t["pending"] = role, reservation["expires"], None
        if not math.isfinite(t["expires"]) or t["expires"] <= now:
            return self.attention(t, "无法确认预约到期时间，请在学校页面核对。")
        other = "B" if role == "A" else "A"
        t["next"] = max(t["expires"] - t["lead"], t["acquired"] + COOLDOWN, self.ready_at(mode, other))
        manual = t.get("manual_handoff")
        if manual:
            t["manual_handoff"] = None
            t["status"] = "paused" if manual["paused"] else "running"
            t["message"] = f"已手动切换至账号 {role}，" + ("轮换仍保持暂停。" if manual["paused"] else "继续自动轮换。")
            self.event(t["message"], t)
            return
        if t["next"] >= t["expires"] - 2:
            return self.attention(t, "接棒账号冷却或持座保护超过剩余预约时间，已保留当前预约。")
        t["status"], t["message"] = "running", f"账号 {role} 持座，等待下一次轮换。"
        if now < t["next"]:
            return
        # The recipient has just been read as logged in, unoccupied and out of cooldown.
        t["pending"] = {"kind": "cancel", "role": role, "time": now}
        self.store.save()  # Durable intent precedes every external mutation.
        p.cancel(role)
        self.store.data["cooldowns"][mode + ":" + role] = self.now(mode) + COOLDOWN
        self.store.save()
        self.acquire(t, other, True)  # No sleep between release acknowledgement and takeover.

    def acquire(self, t, role, handoff):
        t["pending"] = {"kind": "reserve", "role": role, "handoff": handoff, "time": self.now(t["mode"])}
        self.store.save()
        self.providers[t["mode"]].reserve(role, t)
        states = self.observe(t["mode"])
        if not self.matches(t, states[role]["reservation"]):
            t["status"], t["message"] = "recovering", "选座结果尚未确认，正在重新核对实际预约。"
            return
        self.step(t, states)

    def advance_demo(self):
        with self.lock:
            if self.store.data["mode"] != "demo":
                raise Problem("真实模式不能加速时间。")
            self.tick()
            candidates = [t["next"] for t in self.store.data["tasks"] if
                          t["mode"] == "demo" and t["status"] == "running" and t["next"] > 0]
            if not candidates:
                raise Problem("请先创建并启动一个模拟任务。")
            self.store.data["offset"] += max(0, min(candidates) - self.now("demo") + 0.2)
            self.tick()

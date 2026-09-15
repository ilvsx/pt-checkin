"""每日定时调度器。

设计要点
--------
* 每个账号可单独设置签到时间（HH:MM）与随机延迟窗口；未设置则用全局默认。
* 随机延迟按 ``(账号, 日期)`` 做哈希，保证**同一天内多次重启结果一致**，
  不会因为进程重启就漂移到另一个时间点。
* 进程启动时若已过签到时间且当天未签到，会立即补签（catch-up）。
* 失败后按固定间隔重试，达到当日上限后停止，避免无意义刷站。
"""

from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date as _date, datetime, time as _time, timedelta, timezone
from typing import Any

from .checkin import CheckinService
from .logutil import get_logger
from .store import Store, iso, parse_iso, utcnow

log = get_logger("scheduler")

FAILURE_STATUSES = ("failed", "auth_failed", "network_error")
RETRYABLE_STATES = {"due", "waiting_retry"}


def parse_hhmm(value: str | None) -> _time | None:
    """解析 ``HH:MM`` / ``H:MM`` / ``HH:MM:SS``。"""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def stable_jitter(account_id: int, day: str, max_seconds: int) -> int:
    """按账号与日期生成稳定的随机延迟（0..max_seconds）。"""
    if not max_seconds or max_seconds <= 0:
        return 0
    digest = hashlib.sha256(f"ptcheckin:{account_id}:{day}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % (int(max_seconds) + 1)


class Scheduler:
    """后台调度线程 + 工作线程池。"""

    def __init__(self, store: Store, settings, service: CheckinService):
        self.store = store
        self.settings = settings
        self.service = service
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pool: ThreadPoolExecutor | None = None
        self._futures: list[Future] = []
        self._guard = threading.Lock()
        self.last_tick_at: str | None = None
        self.last_action: str | None = None

    # --------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="checkin")
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()
        self.store.kv_set("scheduler_started_at", iso())
        log.info("调度器已启动（tick=%ss）", self.settings.get("scheduler_tick_seconds", 20))

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)
        log.info("调度器已停止")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - 调度线程绝不能挂掉
                log.exception("调度 tick 异常: %s", exc)
            tick = max(5, int(self.settings.get("scheduler_tick_seconds", 20) or 20))
            self._stop.wait(tick)

    # ------------------------------------------------------------------ plan
    def plan(self, account: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
        """计算某个账号当前应处于的状态。

        state 取值：``done`` / ``pending`` / ``due`` / ``waiting_retry`` /
        ``exhausted`` / ``disabled`` / ``invalid_schedule``
        """
        now = now or utcnow()
        tz = self.service.tz_for(account)
        local = now.astimezone(tz)
        today = local.date().isoformat()

        plan: dict[str, Any] = {
            "account_id": account.get("id"),
            "name": account.get("name"),
            "enabled": bool(account.get("enabled")),
            "timezone": str(tz),
            "date": today,
            "state": "pending",
            "target_at": None,
            "next_run_at": None,
            "attempts": 0,
            "failures": 0,
            "signed": False,
        }

        if not account.get("enabled"):
            plan["state"] = "disabled"
            return plan

        schedule_str = account.get("schedule_time") or self.settings.get(
            "default_schedule_time", "00:30"
        )
        hhmm = parse_hhmm(schedule_str)
        if hhmm is None:
            plan["state"] = "invalid_schedule"
            plan["error"] = f"签到时间格式不正确: {schedule_str!r}（应为 HH:MM）"
            return plan

        max_jitter = account.get("jitter_seconds")
        if max_jitter is None:
            max_jitter = self.settings.get("default_jitter_seconds", 600)
        jitter = stable_jitter(int(account.get("id") or 0), today, int(max_jitter or 0))

        target = datetime.combine(local.date(), hhmm, tzinfo=tz) + timedelta(seconds=jitter)
        plan["target_at"] = target.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        plan["jitter_seconds"] = jitter
        plan["schedule_time"] = f"{hhmm.hour:02d}:{hhmm.minute:02d}"

        tomorrow_target = datetime.combine(local.date() + timedelta(days=1), hhmm, tzinfo=tz) + timedelta(
            seconds=stable_jitter(int(account.get("id") or 0), (_date.fromisoformat(today) + timedelta(days=1)).isoformat(), int(max_jitter or 0))
        )

        day = self.store.get_day(int(account["id"]), today) or {}
        plan["signed"] = bool(day.get("signed"))
        plan["points"] = day.get("points")

        if plan["signed"]:
            plan["state"] = "done"
            plan["next_run_at"] = tomorrow_target.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return plan

        attempts = self.store.attempts_on_date(int(account["id"]), today)
        plan["attempts"] = len(attempts)
        failures = [a for a in attempts if a.get("status") in FAILURE_STATUSES]
        plan["failures"] = len(failures)

        max_retries = int(self.settings.get("max_retries_per_day", 5) or 0)
        allowed_failures = 1 + max_retries
        retry_interval = int(self.settings.get("retry_interval_seconds", 900) or 0)

        if local < target:
            plan["state"] = "pending"
            plan["next_run_at"] = target.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return plan

        if len(failures) >= allowed_failures:
            plan["state"] = "exhausted"
            plan["next_run_at"] = tomorrow_target.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return plan

        last_failure = failures[0] if failures else None  # attempts_on_date 按 id 倒序
        last_dt = parse_iso((last_failure or {}).get("started_at")) if last_failure else None
        if last_dt is not None and retry_interval > 0:
            # 重试节奏以"最近一次失败"为基准；若上次是成功/已签到但台账未落库，
            # 说明状态不一致，应立即复核而不是傻等。
            wait_until = last_dt + timedelta(seconds=retry_interval)
            if now < wait_until:
                plan["state"] = "waiting_retry"
                plan["next_run_at"] = wait_until.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                return plan

        plan["state"] = "due"
        plan["next_run_at"] = iso(now)
        plan["notify"] = len(failures) == 0 or (len(failures) + 1) >= allowed_failures
        return plan

    # ------------------------------------------------------------------ tick
    def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """检查所有账号，对到期的账号提交签到任务。返回本次提交的 plan 列表。"""
        now = now or utcnow()
        self.last_tick_at = iso(now)
        if not self.settings.get("scheduler_enabled", True):
            return []

        submitted: list[dict[str, Any]] = []
        self._reap_futures()
        for account in self.store.list_accounts(decrypt=True, only_enabled=True):
            plan = self.plan(account, now)
            if plan.get("state") != "due":
                continue
            self._submit(account, plan)
            submitted.append(plan)
        if submitted:
            self.last_action = f"{iso(now)} 提交 {len(submitted)} 个签到任务"
        return submitted

    def _submit(self, account: dict[str, Any], plan: dict[str, Any]) -> None:
        failures = int(plan.get("failures") or 0)
        trigger = "auto" if failures == 0 else "retry"
        notify = bool(plan.get("notify", True))
        log.info(
            "排队签到: %s (#%s) trigger=%s failures=%s notify=%s",
            account.get("name"),
            account.get("id"),
            trigger,
            failures,
            notify,
        )
        assert self._pool is not None
        future = self._pool.submit(self.service.run, account, trigger, notify=notify)
        with self._guard:
            self._futures.append(future)

    def _reap_futures(self) -> None:
        with self._guard:
            alive = []
            for fut in self._futures:
                if fut.done():
                    exc = fut.exception()
                    if exc:
                        log.error("签到任务异常: %s", exc)
                else:
                    alive.append(fut)
            self._futures = alive

    # --------------------------------------------------------------- status
    def pending_plans(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or utcnow()
        plans = []
        for account in self.store.list_accounts(decrypt=False):
            plan = self.plan(account, now)
            plan["last_attempt"] = self.store.last_attempt(int(account["id"]))
            plans.append(plan)
        return plans

    def next_run_overall(self, now: datetime | None = None) -> str | None:
        now = now or utcnow()
        candidates = []
        for account in self.store.list_accounts(decrypt=False, only_enabled=True):
            plan = self.plan(account, now)
            nxt = plan.get("next_run_at")
            if nxt and plan.get("state") in ("pending", "waiting_retry", "done", "exhausted"):
                candidates.append(nxt)
        return min(candidates) if candidates else None

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "enabled": bool(self.settings.get("scheduler_enabled", True)),
            "tick_seconds": self.settings.get("scheduler_tick_seconds", 20),
            "last_tick_at": self.last_tick_at,
            "last_action": self.last_action,
            "started_at": self.store.kv_get("scheduler_started_at"),
            "next_run_at": self.next_run_overall(),
            "running_accounts": self.service.running_accounts(),
            "server_time_utc": iso(),
        }

    def wait_idle(self, timeout: float = 120) -> None:
        """等待当前所有签到任务结束（CLI/测试用）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._guard:
                pending = [f for f in self._futures if not f.done()]
            if not pending:
                return
            time.sleep(0.1)

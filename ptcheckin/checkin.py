"""签到主流程：一次"抓取 + 解析 + 判定 + 落库 + 通知"。"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .client import SiteClient, parse_cookie_string, validate_cookie
from .logutil import get_logger
from .notify import Notifier
from .parser import AttendancePage, parse_attendance
from .store import Store, iso, utcnow

log = get_logger("checkin")

# 站点记录的 created_at 与我们请求开始时间的允许偏差（秒）
FRESH_TOLERANCE_SECONDS = 20

STATUS_LABELS = {
    "success": "签到成功",
    "already": "今日已签到",
    "failed": "签到失败",
    "auth_failed": "Cookie 失效",
    "network_error": "网络错误",
}


@dataclass
class CheckResult:
    account_id: int | None = None
    account_name: str | None = None
    status: str = "failed"
    trigger: str = "manual"
    is_new: bool = False
    recovered: bool = False
    http_status: int | None = None
    duration_ms: int | None = None
    site_date: str | None = None
    check_date: str | None = None
    points: int | None = None
    streak: int | None = None
    total_count: int | None = None
    rank: int | None = None
    rank_total: int | None = None
    retro_cards: int | None = None
    message: str | None = None
    error: str | None = None
    records_synced: int = 0
    notifications: list[dict[str, Any]] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("success", "already")

    @property
    def label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ok"] = self.ok
        data["label"] = self.label
        return data


class CheckinService:
    """线程安全的签到执行器。"""

    def __init__(self, store: Store, settings, notifier: Notifier | None = None):
        self.store = store
        self.settings = settings
        self.notifier = notifier or Notifier(settings)
        self._locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._running: set[int] = set()

    # ------------------------------------------------------------- helpers
    def _lock_for(self, account_id: int) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(account_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[account_id] = lock
            return lock

    def running_accounts(self) -> list[int]:
        with self._locks_guard:
            return sorted(self._running)

    def tz_for(self, account: dict[str, Any]) -> ZoneInfo:
        name = (account or {}).get("timezone") or self.settings.get("timezone") or "Asia/Shanghai"
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001
            log.warning("未知时区 %r，回退 Asia/Shanghai", name)
            return ZoneInfo("Asia/Shanghai")

    # ---------------------------------------------------------------- run
    def run(
        self,
        account: dict[str, Any],
        trigger: str = "manual",
        *,
        notify: bool = True,
        decrypt_cookie: bool = False,
    ) -> CheckResult:
        """执行一次签到。

        Args:
            account: 账号字典（需包含解密后的 ``cookie``，或 base_url 等基础字段）
            trigger: auto / retry / manual / refresh / cli / startup
        """
        account_id = int(account.get("id") or 0)
        lock = self._lock_for(account_id)
        if not lock.acquire(blocking=False):
            log.info("账号 #%s 正在签到中，跳过本次触发(%s)", account_id, trigger)
            return CheckResult(
                account_id=account_id,
                account_name=account.get("name"),
                status="failed",
                trigger=trigger,
                error="该账号已有签到任务在执行中",
                started_at=iso(),
                finished_at=iso(),
            )
        with self._locks_guard:
            self._running.add(account_id)
        try:
            return self._run_locked(account, trigger, notify=notify)
        finally:
            with self._locks_guard:
                self._running.discard(account_id)
            lock.release()

    def _run_locked(self, account: dict[str, Any], trigger: str, *, notify: bool) -> CheckResult:
        account_id = int(account.get("id") or 0)
        tz = self.tz_for(account)
        started = utcnow()
        started_local = started.astimezone(tz)
        result = CheckResult(
            account_id=account_id,
            account_name=account.get("name"),
            trigger=trigger,
            started_at=iso(started),
        )

        cookie = account.get("cookie") or ""
        if not account.get("base_url"):
            result.status = "failed"
            result.error = "账号未配置站点地址"
            result.finished_at = iso()
            return self._finalize(result, account, notify=notify)

        valid, reason = validate_cookie(cookie)
        if not valid:
            result.status = "auth_failed"
            result.error = reason
            result.finished_at = iso()
            return self._finalize(result, account, notify=notify)

        client = SiteClient(
            account["base_url"],
            cookie,
            user_agent=account.get("user_agent"),
            extra_headers=account.get("extra_headers"),
            timeout=float(self.settings.get("request_timeout", 30)),
            verify_ssl=bool(self.settings.get("verify_ssl", True)),
            proxy=str(self.settings.get("proxy") or ""),
        )

        response = client.attendance()
        result.http_status = response.status
        result.duration_ms = response.elapsed_ms

        if response.error and response.status == 0:
            result.status = "network_error"
            result.error = response.error
            result.finished_at = iso()
            return self._finalize(result, account, notify=notify)

        fallback_date = started_local.strftime("%Y-%m-%d")
        page = parse_attendance(response.text, fallback_date=fallback_date)

        if not page.authenticated:
            result.status = "auth_failed"
            result.error = page.error or "Cookie 已失效或站点结构变化"
            result.site_date = page.site_date or fallback_date
            result.check_date = result.site_date
            result.finished_at = iso()
            return self._finalize(result, account, notify=notify, page=page)

        site_date = page.site_date or fallback_date
        result.site_date = site_date
        result.check_date = site_date
        result.message = page.message
        result.total_count = page.total_count
        result.streak = page.streak
        result.points = page.points
        result.retro_cards = page.retro_cards
        result.rank = page.rank
        result.rank_total = page.rank_total

        # 1) 判定前先记录"本次执行之前"本地是否已有今日签到记录。
        #    必须在同步台账之前读取，否则同步动作会把它变成 True。
        previously_signed = bool((self.store.get_day(account_id, site_date) or {}).get("signed"))
        record = page.records.get(site_date)

        # 2) 把站点台账同步进本地"每日台账"（精确判断的历史依据）
        result.records_synced = self._sync_ledger(account_id, page)

        # 3) 判定今日状态
        if page.signed_today:
            fresh = self._detect_fresh(record, tz, started_local, result.finished_at)
            # 只有"站点记录时间落在本次请求附近"且"本次之前本地尚无今日记录"
            # 才算真正由本系统完成了签到；否则视为今日已签到。
            if fresh is None:
                is_new = not previously_signed
            else:
                is_new = bool(fresh) and not previously_signed
            result.is_new = is_new
            result.status = "success" if result.is_new else "already"
            self.store.upsert_day(
                account_id,
                site_date,
                signed=True,
                points=record.points if record else result.points,
                streak=page.streak,
                is_retroactive=record.is_retroactive if record else 0,
                site_created_at=record.created_at if record else None,
                source="site",
            )
            if self.store.count_attempts(
                account_id, site_date, statuses=("failed", "auth_failed", "network_error")
            ):
                result.recovered = result.is_new
        else:
            result.status = "failed"
            result.error = "站点未返回今日签到记录，签到可能未生效"
            self.store.upsert_day(account_id, site_date, signed=False, source="local")

        # 3) 站点未提供连续天数时本地推算
        if result.streak is None:
            result.streak = self.store.compute_streak(account_id, site_date)

        result.finished_at = iso()
        return self._finalize(result, account, notify=notify, page=page)

    # ------------------------------------------------------------ internals
    def _sync_ledger(self, account_id: int, page: AttendancePage) -> int:
        count = 0
        for date_str, record in page.records.items():
            self.store.upsert_day(
                account_id,
                date_str,
                signed=True,
                points=record.points,
                is_retroactive=record.is_retroactive,
                site_created_at=record.created_at,
                source="site",
            )
            count += 1
        return count

    @staticmethod
    def _detect_fresh(
        record: Any, tz: ZoneInfo, started_local: datetime, finished_at: str | None
    ) -> bool | None:
        """通过站点记录时间判断"本次请求是否真的完成了签到"。

        返回 True/False；站点时间无法解析时返回 None。
        """
        created = getattr(record, "created_at", None) if record else None
        if not created:
            return None
        try:
            site_dt = datetime.strptime(created.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
        except (ValueError, AttributeError):
            return None
        threshold = started_local - timedelta(seconds=FRESH_TOLERANCE_SECONDS)
        # 站点时间比本地时间早太多说明是历史记录；反之可能有时钟漂移，做双向容忍
        return site_dt >= threshold

    def _finalize(
        self,
        result: CheckResult,
        account: dict[str, Any],
        *,
        notify: bool,
        page: AttendancePage | None = None,
    ) -> CheckResult:
        if result.finished_at is None:
            result.finished_at = iso()
        snippet = self._build_snippet(result, page)
        result.account_name = account.get("name") or result.account_name
        attempt_id = self.store.add_attempt(
            {
                "account_id": result.account_id,
                "account_name": result.account_name,
                "check_date": result.check_date,
                "trigger": result.trigger,
                "status": result.status,
                "is_new": 1 if result.is_new else 0,
                "http_status": result.http_status,
                "duration_ms": result.duration_ms,
                "points": result.points,
                "streak": result.streak,
                "total_count": result.total_count,
                "rank": result.rank,
                "rank_total": result.rank_total,
                "retro_cards": result.retro_cards,
                "message": result.message,
                "error": result.error,
                "site_date": result.site_date,
                "started_at": result.started_at,
                "finished_at": result.finished_at,
                "raw_snippet": snippet,
            }
        )
        log.info(
            "账号 #%s(%s) %s -> %s%s",
            result.account_id,
            result.account_name,
            result.trigger,
            result.label,
            f" | {result.error}" if result.error else "",
        )
        if notify:
            try:
                notes = self.notifier.notify_checkin(result, account)
                result.notifications = [n.to_dict() for n in notes]
                log.debug("attempt #%s 通知结果: %s", attempt_id, result.notifications)
            except Exception as exc:  # noqa: BLE001
                log.warning("通知流程异常: %s", exc)
        return result

    @staticmethod
    def _build_snippet(result: CheckResult, page: AttendancePage | None) -> str:
        payload: dict[str, Any] = {
            "status": result.status,
            "site_date": result.site_date,
            "http_status": result.http_status,
        }
        if page is not None:
            payload["page"] = page.to_dict()
        if result.error:
            payload["error"] = result.error
        return json.dumps(payload, ensure_ascii=False)[:2000]


def cookie_summary(cookie: str) -> dict[str, Any]:
    jar = parse_cookie_string(cookie)
    return {
        "keys": sorted(jar.keys()),
        "uid": jar.get("c_secure_uid", ""),
        "has_pass": bool(jar.get("c_secure_pass")),
    }

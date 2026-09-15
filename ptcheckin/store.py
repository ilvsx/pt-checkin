"""SQLite 持久层：账号、每日签到台账、每次尝试记录。"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .logutil import get_logger
from .secretsbox import SecretsBox

log = get_logger("store")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL UNIQUE,
    site          TEXT    NOT NULL DEFAULT 'hhanclub',
    base_url      TEXT    NOT NULL,
    cookie_enc    TEXT    NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    schedule_time TEXT,
    jitter_seconds INTEGER,
    timezone      TEXT,
    user_agent    TEXT,
    extra_headers TEXT,
    note          TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

-- 精确到"天"的签到台账：以站点自身日历数据为准
CREATE TABLE IF NOT EXISTS checkin_days (
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    check_date      TEXT    NOT NULL,              -- YYYY-MM-DD（站点时区）
    signed          INTEGER NOT NULL DEFAULT 0,
    points          INTEGER,
    streak          INTEGER,
    is_retroactive  INTEGER NOT NULL DEFAULT 0,
    site_created_at TEXT,                          -- 站点记录的签到时间
    source          TEXT    NOT NULL DEFAULT 'site',
    first_seen_at   TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    PRIMARY KEY (account_id, check_date)
);

-- 每一次 HTTP 尝试的完整审计记录
CREATE TABLE IF NOT EXISTS checkin_attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    account_name  TEXT,
    check_date    TEXT,                            -- 站点日期
    trigger       TEXT    NOT NULL,                -- auto|retry|manual|refresh|cli|startup
    status        TEXT    NOT NULL,                -- success|already|failed|auth_failed|network_error
    is_new        INTEGER NOT NULL DEFAULT 0,      -- 本次是否真的产生了签到
    http_status   INTEGER,
    duration_ms   INTEGER,
    points        INTEGER,
    points_unit   TEXT,
    streak        INTEGER,
    total_count   INTEGER,
    rank          INTEGER,
    rank_total    INTEGER,
    retro_cards   INTEGER,
    message       TEXT,
    error         TEXT,
    site_date     TEXT,
    started_at    TEXT    NOT NULL,
    finished_at   TEXT    NOT NULL,
    raw_snippet   TEXT
);

CREATE INDEX IF NOT EXISTS idx_attempts_account_time ON checkin_attempts(account_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_attempts_time         ON checkin_attempts(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_days_account_date      ON checkin_days(account_id, check_date);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


# 后续新增的列：对既有数据库做幂等迁移（见 Store._migrate）
MIGRATIONS: dict[str, dict[str, str]] = {
    "checkin_attempts": {"points_unit": "TEXT"},
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    """统一的 UTC ISO8601 存储格式：2026-09-15T03:11:08Z"""
    dt = dt or utcnow()
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None


class Store:
    """线程安全的 SQLite 封装（单连接 + 互斥锁，够用且简单）。"""

    def __init__(self, db_path: Path, secrets: SecretsBox):
        self.db_path = Path(db_path)
        self.secrets = secrets
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
        self._migrate()
        self._harden_permissions()

    def _migrate(self) -> None:
        """轻量迁移：给既有数据库补上后加的列（幂等）。"""
        for table, columns in MIGRATIONS.items():
            existing = {row["name"] for row in self._query(f"PRAGMA table_info({table})")}
            for column, decl in columns.items():
                if column not in existing:
                    self._exec(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                    log.info("数据库迁移：%s 新增列 %s", table, column)

    def _harden_permissions(self) -> None:
        """数据库内含 Cookie 密文，限制为仅属主可读写。"""
        import os

        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.db_path) + suffix)
            try:
                if path.exists():
                    os.chmod(path, 0o600)
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------- helpers
    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, tuple(params)).fetchall())

    def _one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------ accounts
    @staticmethod
    def _account_row_to_dict(row: sqlite3.Row, secrets: SecretsBox, decrypt: bool) -> dict[str, Any]:
        d = dict(row)
        token = d.pop("cookie_enc", "") or ""
        cookie = secrets.decrypt(token) if token else ""
        d["enabled"] = bool(d.get("enabled"))
        d["has_cookie"] = bool(token)
        # 脱敏展示始终可用；完整明文仅在明确要求时返回
        d["cookie_masked"] = _mask_cookie(cookie)
        try:
            d["extra_headers"] = json.loads(d.get("extra_headers") or "{}")
        except (TypeError, json.JSONDecodeError):
            d["extra_headers"] = {}
        if decrypt:
            d["cookie"] = cookie
        return d

    def create_account(self, data: dict[str, Any]) -> dict[str, Any]:
        now = iso()
        cookie = (data.get("cookie") or "").strip()
        cur = self._exec(
            """
            INSERT INTO accounts (name, site, base_url, cookie_enc, enabled, schedule_time,
                                  jitter_seconds, timezone, user_agent, extra_headers, note,
                                  created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                (data.get("name") or "").strip(),
                (data.get("site") or "hhanclub").strip(),
                (data.get("base_url") or "").rstrip("/"),
                self.secrets.encrypt(cookie),
                1 if data.get("enabled", True) else 0,
                (data.get("schedule_time") or None),
                data.get("jitter_seconds"),
                (data.get("timezone") or None),
                data.get("user_agent") or None,
                json.dumps(data.get("extra_headers") or {}, ensure_ascii=False),
                data.get("note") or None,
                now,
                now,
            ),
        )
        return self.get_account(int(cur.lastrowid), decrypt=True)  # type: ignore[arg-type]

    ALLOWED_ACCOUNT_FIELDS = {
        "name",
        "site",
        "base_url",
        "enabled",
        "schedule_time",
        "jitter_seconds",
        "timezone",
        "user_agent",
        "extra_headers",
        "note",
    }

    def update_account(self, account_id: int, patch: dict[str, Any]) -> dict[str, Any] | None:
        fields: list[str] = []
        params: list[Any] = []
        for key, value in (patch or {}).items():
            if key == "cookie":
                fields.append("cookie_enc = ?")
                params.append(self.secrets.encrypt((value or "").strip()))
            elif key in self.ALLOWED_ACCOUNT_FIELDS:
                if key == "enabled":
                    value = 1 if value else 0
                elif key == "extra_headers":
                    value = json.dumps(value or {}, ensure_ascii=False)
                elif key in ("schedule_time", "timezone", "user_agent", "note") and value == "":
                    value = None
                fields.append(f"{key} = ?")
                params.append(value)
        if not fields:
            return self.get_account(account_id, decrypt=True)
        fields.append("updated_at = ?")
        params.append(iso())
        params.append(account_id)
        self._exec(f"UPDATE accounts SET {', '.join(fields)} WHERE id = ?", params)
        return self.get_account(account_id, decrypt=True)

    def get_account(self, account_id: int, decrypt: bool = False) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        return self._account_row_to_dict(row, self.secrets, decrypt) if row else None

    def get_account_by_name(self, name: str, decrypt: bool = False) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM accounts WHERE name = ?", (name,))
        return self._account_row_to_dict(row, self.secrets, decrypt) if row else None

    def list_accounts(self, decrypt: bool = False, only_enabled: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM accounts"
        if only_enabled:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id ASC"
        return [self._account_row_to_dict(r, self.secrets, decrypt) for r in self._query(sql)]

    def delete_account(self, account_id: int) -> bool:
        cur = self._exec("DELETE FROM accounts WHERE id = ?", (account_id,))
        return cur.rowcount > 0

    # --------------------------------------------------------- checkin days
    def upsert_day(
        self,
        account_id: int,
        check_date: str,
        *,
        signed: bool,
        points: int | None = None,
        streak: int | None = None,
        is_retroactive: int = 0,
        site_created_at: str | None = None,
        source: str = "site",
    ) -> None:
        now = iso()
        self._exec(
            """
            INSERT INTO checkin_days (account_id, check_date, signed, points, streak,
                                      is_retroactive, site_created_at, source,
                                      first_seen_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account_id, check_date) DO UPDATE SET
                signed          = MAX(checkin_days.signed, excluded.signed),
                points          = COALESCE(excluded.points, checkin_days.points),
                streak          = COALESCE(excluded.streak, checkin_days.streak),
                is_retroactive  = excluded.is_retroactive,
                site_created_at = COALESCE(excluded.site_created_at, checkin_days.site_created_at),
                source          = excluded.source,
                updated_at      = excluded.updated_at
            """,
            (
                account_id,
                check_date,
                1 if signed else 0,
                points,
                streak,
                int(is_retroactive or 0),
                site_created_at,
                source,
                now,
                now,
            ),
        )

    def sync_ledger(self, account_id: int, records: dict[str, Any]) -> tuple[int, int]:
        """用站点台账**权威地**覆盖同步某个账号的签到日期。

        ``upsert_day`` 用 ``MAX(signed)`` 保证"已签到"不会被瞬时误读抹掉，
        但也因此无法纠正历史上被误判为已签到的日期。站点台账是权威事实，
        所以这里额外删除"台账覆盖范围内、本次却未出现"的日期。

        只清理台账窗口 ``[最早日期, 最晚日期]`` 之内，窗口之外的历史保留。

        Args:
            records: ``{日期: AttendanceRecord}``
        Returns:
            ``(同步条数, 清理条数)``
        """
        if not records:
            return 0, 0
        dates = sorted(records)
        lo, hi = dates[0], dates[-1]

        existing = {
            row["check_date"]
            for row in self._query(
                "SELECT check_date FROM checkin_days WHERE account_id = ? AND signed = 1 "
                "AND check_date BETWEEN ? AND ?",
                (account_id, lo, hi),
            )
        }
        stale = sorted(existing - set(dates))
        for date_str in stale:
            self._exec(
                "DELETE FROM checkin_days WHERE account_id = ? AND check_date = ?",
                (account_id, date_str),
            )

        for date_str, record in records.items():
            self.upsert_day(
                account_id,
                date_str,
                signed=True,
                points=getattr(record, "points", None),
                is_retroactive=getattr(record, "is_retroactive", 0) or 0,
                site_created_at=getattr(record, "created_at", None),
                source="site",
            )
        if stale:
            log.info(
                "账号 #%s 台账同步：清理了 %d 个不再属于台账的日期 %s",
                account_id, len(stale), stale[:5],
            )
        return len(records), len(stale)

    def get_day(self, account_id: int, check_date: str) -> dict[str, Any] | None:
        row = self._one(
            "SELECT * FROM checkin_days WHERE account_id = ? AND check_date = ?",
            (account_id, check_date),
        )
        return dict(row) if row else None

    def list_days(
        self, account_id: int, date_from: str | None = None, date_to: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM checkin_days WHERE account_id = ?"
        params: list[Any] = [account_id]
        if date_from:
            sql += " AND check_date >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND check_date <= ?"
            params.append(date_to)
        sql += " ORDER BY check_date DESC"
        return [dict(r) for r in self._query(sql, params)]

    def month_days(self, account_id: int, month: str) -> list[dict[str, Any]]:
        """month: YYYY-MM"""
        return [
            dict(r)
            for r in self._query(
                "SELECT * FROM checkin_days WHERE account_id = ? AND check_date LIKE ? ORDER BY check_date ASC",
                (account_id, f"{month}-%"),
            )
        ]

    def signed_dates(
        self, account_id: int, date_from: str, date_to: str, exclude_retroactive: bool = True
    ) -> set[str]:
        """已签到的日期集合。

        默认排除补签（``is_retroactive=1``）：站点计算"连续签到"时不计补签
        （实测补签次日连续奖励会重置），因此本地推算必须保持一致。
        """
        sql = (
            "SELECT check_date FROM checkin_days WHERE account_id = ? AND signed = 1 "
            "AND check_date BETWEEN ? AND ?"
        )
        if exclude_retroactive:
            sql += " AND COALESCE(is_retroactive, 0) = 0"
        rows = self._query(sql, (account_id, date_from, date_to))
        return {r["check_date"] for r in rows}

    def compute_streak(self, account_id: int, today: str, lookback_days: int = 400) -> int:
        """计算截至 today（含）的连续签到天数。"""
        try:
            end = _date.fromisoformat(today)
        except ValueError:
            return 0
        start = end - timedelta(days=lookback_days)
        signed = self.signed_dates(account_id, start.isoformat(), end.isoformat())
        streak = 0
        cursor = end
        # 今天若尚未签到，则从昨天往前算，连续天数仍然成立
        if cursor.isoformat() not in signed:
            cursor -= timedelta(days=1)
        while cursor.isoformat() in signed:
            streak += 1
            cursor -= timedelta(days=1)
            if streak > lookback_days:
                break
        return streak

    # ------------------------------------------------------------ attempts
    def add_attempt(self, data: dict[str, Any]) -> int:
        cols = [
            "account_id", "account_name", "check_date", "trigger", "status", "is_new",
            "http_status", "duration_ms", "points", "points_unit", "streak", "total_count", "rank",
            "rank_total", "retro_cards", "message", "error", "site_date", "started_at",
            "finished_at", "raw_snippet",
        ]
        values = []
        for col in cols:
            value = data.get(col)
            if value is None and col == "is_new":
                value = 0
            values.append(value)
        cur = self._exec(
            f"INSERT INTO checkin_attempts ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            values,
        )
        return int(cur.lastrowid)  # type: ignore[arg-type]

    def list_attempts(
        self,
        account_id: int | None = None,
        *,
        status: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[Any] = []
        if account_id:
            where.append("account_id = ?")
            params.append(account_id)
        if status:
            where.append("status = ?")
            params.append(status)
        if date_from:
            where.append("substr(started_at,1,10) >= ?")
            params.append(date_from)
        if date_to:
            where.append("substr(started_at,1,10) <= ?")
            params.append(date_to)
        clause = f" WHERE {' AND '.join(where)}" if where else ""

        total_row = self._one(f"SELECT COUNT(*) AS n FROM checkin_attempts{clause}", params)
        total = int(total_row["n"]) if total_row else 0

        page = max(1, int(page))
        page_size = max(1, min(200, int(page_size)))
        rows = self._query(
            f"SELECT * FROM checkin_attempts{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        )
        return [dict(r) for r in rows], total

    def attempts_on_date(self, account_id: int, check_date: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._query(
                "SELECT * FROM checkin_attempts WHERE account_id = ? AND check_date = ? "
                "ORDER BY id DESC",
                (account_id, check_date),
            )
        ]

    def count_attempts(
        self, account_id: int, check_date: str, statuses: tuple[str, ...] | None = None
    ) -> int:
        sql = "SELECT COUNT(*) AS n FROM checkin_attempts WHERE account_id = ? AND check_date = ?"
        params: list[Any] = [account_id, check_date]
        if statuses:
            sql += f" AND status IN ({', '.join('?' * len(statuses))})"
            params.extend(statuses)
        row = self._one(sql, params)
        return int(row["n"]) if row else 0

    def last_attempt(self, account_id: int, check_date: str | None = None) -> dict[str, Any] | None:
        if check_date:
            row = self._one(
                "SELECT * FROM checkin_attempts WHERE account_id = ? AND check_date = ? "
                "ORDER BY id DESC LIMIT 1",
                (account_id, check_date),
            )
        else:
            row = self._one(
                "SELECT * FROM checkin_attempts WHERE account_id = ? ORDER BY id DESC LIMIT 1",
                (account_id,),
            )
        return dict(row) if row else None

    def last_success_date(self, account_id: int) -> str | None:
        row = self._one(
            "SELECT check_date FROM checkin_attempts WHERE account_id = ? AND is_new = 1 "
            "ORDER BY id DESC LIMIT 1",
            (account_id,),
        )
        return row["check_date"] if row else None

    def purge_raw(self, keep_days: int) -> int:
        if keep_days is None or keep_days < 0:
            return 0
        cutoff = iso(utcnow() - timedelta(days=keep_days))
        cur = self._exec(
            "UPDATE checkin_attempts SET raw_snippet = NULL "
            "WHERE raw_snippet IS NOT NULL AND started_at < ?",
            (cutoff,),
        )
        return cur.rowcount or 0

    # ------------------------------------------------------------------ kv
    def kv_set(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self._one("SELECT value FROM kv WHERE key = ?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return default

    # --------------------------------------------------------------- stats
    def stats(self, today: str) -> dict[str, Any]:
        accounts = self.list_accounts()
        enabled = [a for a in accounts if a["enabled"]]
        signed = failed = pending = 0
        for acct in enabled:
            day = self.get_day(acct["id"], today)
            if day and day["signed"]:
                signed += 1
                continue
            last = self.last_attempt(acct["id"], today)
            if last and last["status"] in ("failed", "auth_failed", "network_error"):
                failed += 1
            else:
                pending += 1
        total_row = self._one("SELECT COUNT(*) AS n FROM checkin_attempts")
        return {
            "accounts_total": len(accounts),
            "accounts_enabled": len(enabled),
            "today_signed": signed,
            "today_failed": failed,
            "today_pending": pending,
            "attempts_total": int(total_row["n"]) if total_row else 0,
            "date": today,
        }


def _mask_cookie(cookie: str) -> str:
    """脱敏显示：保留少量前缀，隐藏主体。"""
    if not cookie:
        return ""
    parts = [p.strip() for p in cookie.split(";") if p.strip()]
    masked = []
    for part in parts:
        if "=" in part:
            k, _, v = part.partition("=")
            k = k.strip()
            v = v.strip()
            if k in ("c_secure_uid",):
                masked.append(f"{k}={v}")
            else:
                masked.append(f"{k}={'*' * min(len(v), 8)}")
        else:
            masked.append(part)
    return "; ".join(masked)

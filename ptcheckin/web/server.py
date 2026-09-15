"""Web 服务与 JSON API。

使用标准库 ``http.server.ThreadingHTTPServer``，无第三方依赖。
"""

from __future__ import annotations

import json
import mimetypes
import re
import threading
import urllib.parse
from datetime import date as _date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..checkin import CheckResult, CheckinService
from ..client import validate_cookie
from ..curlparse import parse_curl
from ..logutil import get_logger
from ..notify import Notifier
from ..parser import normalize_date
from ..scheduler import Scheduler, parse_hhmm, stable_jitter
from ..store import Store, iso, utcnow

log = get_logger("web")

STATIC_DIR = Path(__file__).resolve().parent / "static"

STATUS_VIEW = {
    "success": ("signed", "已签到"),
    "already": ("signed", "今日已签到"),
    "failed": ("failed", "签到失败"),
    "auth_failed": ("auth_failed", "Cookie 失效"),
    "network_error": ("network_error", "网络错误"),
}


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


class Api:
    """所有业务接口的实现（与 HTTP 层解耦，方便单测）。"""

    def __init__(self, store: Store, settings, service: CheckinService, scheduler: Scheduler):
        self.store = store
        self.settings = settings
        self.service = service
        self.scheduler = scheduler
        self.notifier = service.notifier

    # ------------------------------------------------------------- helpers
    def _now(self) -> datetime:
        return utcnow()

    def _day_for(self, account: dict[str, Any], now: datetime | None = None) -> str:
        tz = self.service.tz_for(account)
        return (now or self._now()).astimezone(tz).date().isoformat()

    def account_view(self, account: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
        """账号 + 今日状态 + 下次运行时间的聚合视图。"""
        now = now or self._now()
        plan = self.scheduler.plan(account, now)
        today = plan.get("date") or self._day_for(account, now)
        day = self.store.get_day(int(account["id"]), today) or {}
        last = self.store.last_attempt(int(account["id"]), today) or self.store.last_attempt(
            int(account["id"])
        )

        if not account.get("enabled"):
            view_status, view_label = "disabled", "已停用"
        elif day.get("signed"):
            view_status, view_label = "signed", "今日已签到"
        elif plan.get("state") == "invalid_schedule":
            view_status, view_label = "failed", "时间配置错误"
        elif plan.get("state") == "exhausted":
            view_status, view_label = "failed", "今日重试已用尽"
        elif plan.get("failures"):
            view_status, view_label = "retrying", "等待重试"
        else:
            view_status, view_label = "pending", "待签到"

        if last:
            last_status = STATUS_VIEW.get(last.get("status"), (last.get("status"), last.get("status")))
            last["view_status"], last["view_label"] = last_status
            last["notifications"] = []

        out = dict(account)
        out.update(
            {
                "today": today,
                "today_signed": bool(day.get("signed")),
                "today_points": day.get("points"),
                "today_site_created_at": day.get("site_created_at"),
                "streak": self.store.compute_streak(int(account["id"]), today),
                "total_signed": self._total_signed(int(account["id"])),
                "view_status": view_status,
                "view_label": view_label,
                "plan": plan,
                "next_run_at": plan.get("next_run_at"),
                "last_attempt": last,
                "has_cookie": bool(account.get("cookie_masked")),
            }
        )
        return out

    def _total_signed(self, account_id: int) -> int:
        rows = self.store._query(  # noqa: SLF001 - 同包内部使用
            "SELECT COUNT(*) AS n FROM checkin_days WHERE account_id = ? AND signed = 1",
            (account_id,),
        )
        return int(rows[0]["n"]) if rows else 0

    # ----------------------------------------------------------- endpoints
    def overview(self) -> dict[str, Any]:
        now = self._now()
        accounts = [self.account_view(a, now) for a in self.store.list_accounts()]
        tz_name = self.settings.get("timezone", "Asia/Shanghai")
        try:
            local_now = now.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:  # noqa: BLE001
            local_now = now.strftime("%Y-%m-%d %H:%M:%S")
        today = now.astimezone(self._tz()).date().isoformat()
        return {
            "server_time_utc": iso(now),
            "server_time_local": local_now,
            "timezone": tz_name,
            "stats": self.store.stats(today),
            "accounts": accounts,
            "scheduler": self.scheduler.status(),
            "site": {"key": "hhanclub", "name": "HHClub", "checkin_path": "attendance.php"},
        }

    def _tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.settings.get("timezone", "Asia/Shanghai"))
        except Exception:  # noqa: BLE001
            return ZoneInfo("Asia/Shanghai")

    def list_accounts(self) -> dict[str, Any]:
        now = self._now()
        return {"accounts": [self.account_view(a, now) for a in self.store.list_accounts()]}

    def get_account(self, account_id: int) -> dict[str, Any]:
        account = self.store.get_account(account_id)
        if not account:
            raise ApiError("账号不存在", 404)
        return {"account": self.account_view(account)}

    def create_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = self._normalize_account_payload(payload, for_create=True)
        existing = self.store.get_account_by_name(data["name"])
        if existing:
            raise ApiError(f"账号名称已存在: {data['name']}")
        account = self.store.create_account(data)
        verify = None
        if payload.get("verify", True):
            verify = self._run_check(account, trigger="refresh")
        view = self.account_view(self.store.get_account(account["id"]) or account)
        return {"account": view, "verify": verify}

    def update_account(self, account_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.store.get_account(account_id):
            raise ApiError("账号不存在", 404)
        patch = self._normalize_account_payload(payload, for_create=False)
        if "name" in patch and patch["name"]:
            other = self.store.get_account_by_name(patch["name"])
            if other and int(other["id"]) != int(account_id):
                raise ApiError(f"账号名称已存在: {patch['name']}")
        account = self.store.update_account(account_id, patch)
        return {"account": self.account_view(account or {})}

    def delete_account(self, account_id: int) -> dict[str, Any]:
        if not self.store.delete_account(account_id):
            raise ApiError("账号不存在", 404)
        return {"deleted": account_id}

    def checkin(self, account_id: int, trigger: str = "manual") -> dict[str, Any]:
        account = self.store.get_account(account_id, decrypt=True)
        if not account:
            raise ApiError("账号不存在", 404)
        result = self._run_check(account, trigger=trigger)
        return {"result": result, "account": None if result is None else self.account_view(
            self.store.get_account(account_id) or account
        )}

    def checkin_all(self, trigger: str = "manual") -> dict[str, Any]:
        accounts = self.store.list_accounts(decrypt=True, only_enabled=True)
        if not accounts:
            return {"queued": 0}
        results: list[dict[str, Any]] = []
        lock = threading.Lock()

        def worker(acct: dict[str, Any]) -> None:
            res = self.service.run(acct, trigger, notify=True)
            with lock:
                results.append(res.to_dict())

        threads = [threading.Thread(target=worker, args=(a,), daemon=True) for a in accounts]
        for t in threads:
            t.start()
        return {"queued": len(threads)}

    def refresh(self, account_id: int) -> dict[str, Any]:
        return self.checkin(account_id, trigger="refresh")

    def calendar(self, account_id: int, month: str) -> dict[str, Any]:
        account = self.store.get_account(account_id)
        if not account:
            raise ApiError("账号不存在", 404)
        if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
            month = self._day_for(account)[:7]
        days = self.store.month_days(account_id, month)
        by_date = {d["check_date"]: d for d in days}
        signed_days = [d for d in days if d["signed"]]
        points = sum(int(d.get("points") or 0) for d in signed_days)
        return {
            "month": month,
            "days": by_date,
            "summary": {
                "signed": len(signed_days),
                "points": points,
                "retroactive": sum(1 for d in signed_days if d.get("is_retroactive")),
            },
        }

    def records(
        self,
        account_id: int | None,
        *,
        status: str | None,
        date_from: str | None,
        date_to: str | None,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        items, total = self.store.list_attempts(
            account_id,
            status=status or None,
            date_from=date_from or None,
            date_to=date_to or None,
            page=page,
            page_size=page_size,
        )
        for item in items:
            view = STATUS_VIEW.get(item.get("status"), (item.get("status"), item.get("status")))
            item["view_status"], item["view_label"] = view
            if item.get("raw_snippet"):
                try:
                    item["detail"] = json.loads(item["raw_snippet"])
                except (TypeError, json.JSONDecodeError):
                    item["detail"] = None
            else:
                item["detail"] = None
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max(1, (total + page_size - 1) // page_size) if page_size else 1,
        }

    def days(self, account_id: int, date_from: str | None, date_to: str | None) -> dict[str, Any]:
        if not self.store.get_account(account_id):
            raise ApiError("账号不存在", 404)
        return {"days": self.store.list_days(account_id, date_from, date_to)}

    def get_settings(self) -> dict[str, Any]:
        data = self.settings.as_dict()
        data["data_dir"] = str(self.settings.data_dir)
        data["encryption"] = getattr(self.store.secrets, "backend", "unknown")
        return {"settings": data}

    def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        patch: dict[str, Any] = {}
        for key in (
            "timezone",
            "default_schedule_time",
            "default_jitter_seconds",
            "max_retries_per_day",
            "retry_interval_seconds",
            "request_timeout",
            "verify_ssl",
            "proxy",
            "scheduler_enabled",
            "scheduler_tick_seconds",
            "keep_raw_days",
        ):
            if key in payload:
                patch[key] = payload[key]

        if "default_schedule_time" in patch and parse_hhmm(patch["default_schedule_time"]) is None:
            raise ApiError("默认签到时间格式应为 HH:MM")
        if "timezone" in patch:
            try:
                ZoneInfo(str(patch["timezone"]))
            except Exception as exc:  # noqa: BLE001
                raise ApiError(f"未知时区: {patch['timezone']}") from exc
        for key in ("default_jitter_seconds", "max_retries_per_day", "retry_interval_seconds",
                    "request_timeout", "scheduler_tick_seconds", "keep_raw_days"):
            if key in patch:
                try:
                    patch[key] = max(0, int(patch[key]))
                except (TypeError, ValueError) as exc:
                    raise ApiError(f"{key} 必须是整数") from exc

        if isinstance(payload.get("web"), dict):
            web_patch = dict(payload["web"])
            if "port" in web_patch:
                try:
                    port = int(web_patch["port"])
                except (TypeError, ValueError) as exc:
                    raise ApiError("端口必须是整数") from exc
                if not (1 <= port <= 65535):
                    raise ApiError("端口范围应为 1-65535")
                web_patch["port"] = port
            patch["web"] = web_patch

        if isinstance(payload.get("notify"), dict):
            patch["notify"] = self._clean_notify(payload["notify"])

        self.settings.update(patch)
        self.settings.save()
        return self.get_settings()

    @staticmethod
    def _clean_notify(notify: dict[str, Any]) -> dict[str, Any]:
        out = dict(notify)
        channels = []
        for item in out.get("channels") or []:
            if not isinstance(item, dict):
                continue
            ch = {k: v for k, v in item.items() if v is not None}
            if ch.get("type"):
                ch["enabled"] = bool(ch.get("enabled", True))
                channels.append(ch)
        out["channels"] = channels
        return out

    def test_notify(self) -> dict[str, Any]:
        title = "[PT签到] 测试通知"
        body = "如果你看到这条消息，说明通知渠道配置正确。\n\n— PT 自动签到系统"
        results = self.notifier.send(title, body, event="test")
        if not results:
            raise ApiError("未配置任何通知渠道，或通知功能未启用")
        return {"results": [r.to_dict() for r in results]}

    def curl_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        parsed = parse_curl(payload.get("curl") or "")
        cookie = parsed.get("cookie", "")
        ok, reason = (False, "未解析到 Cookie")
        if cookie:
            ok, reason = validate_cookie(cookie)
            parsed["cookie_keys"] = sorted(
                {p.split("=", 1)[0].strip() for p in cookie.split(";") if "=" in p}
            )
        parsed["cookie_valid"] = ok
        parsed["cookie_check"] = reason
        parsed["cookie_preview"] = _mask(cookie)
        # 明文 Cookie 仅在显式要求时返回（用于表单回填），避免无意中泄漏到日志/缓存
        if payload.get("include_cookie"):
            parsed["cookie_full"] = cookie
        parsed.pop("cookie", None)
        return {"parsed": parsed}

    def scheduler_tick(self) -> dict[str, Any]:
        submitted = self.scheduler.tick()
        return {"submitted": submitted, "status": self.scheduler.status()}

    def purge(self, keep_days: int | None = None) -> dict[str, Any]:
        days = int(keep_days if keep_days is not None else self.settings.get("keep_raw_days", 30))
        removed = self.store.purge_raw(days)
        return {"purged": removed, "keep_days": days}

    # ------------------------------------------------------------ internals
    def _run_check(self, account: dict[str, Any], trigger: str) -> dict[str, Any]:
        result: CheckResult = self.service.run(account, trigger, notify=True)
        return result.to_dict()

    def _normalize_account_payload(
        self, payload: dict[str, Any], *, for_create: bool
    ) -> dict[str, Any]:
        data: dict[str, Any] = {}

        curl_text = (payload.get("curl") or "").strip()
        if curl_text:
            parsed = parse_curl(curl_text)
            payload = dict(payload)
            payload.setdefault("base_url", parsed.get("base_url"))
            payload.setdefault("cookie", parsed.get("cookie"))
            payload.setdefault("user_agent", parsed.get("user_agent"))
            if not payload.get("name") and parsed.get("site"):
                payload["name"] = parsed["site"].upper()

        if "name" in payload:
            data["name"] = (payload.get("name") or "").strip()
        if "site" in payload:
            data["site"] = (payload.get("site") or "hhanclub").strip()
        if "base_url" in payload:
            base_url = (payload.get("base_url") or "").strip().rstrip("/")
            if base_url and not base_url.startswith(("http://", "https://")):
                raise ApiError("站点地址必须以 http:// 或 https:// 开头")
            data["base_url"] = base_url
        if "cookie" in payload and payload["cookie"] is not None:
            cookie = (payload.get("cookie") or "").strip()
            if cookie:
                ok, reason = validate_cookie(cookie)
                if not ok:
                    raise ApiError(f"Cookie 校验失败: {reason}")
                data["cookie"] = cookie
        if "schedule_time" in payload:
            value = (payload.get("schedule_time") or "").strip()
            if value and parse_hhmm(value) is None:
                raise ApiError("签到时间格式应为 HH:MM")
            data["schedule_time"] = value or None
        if "jitter_seconds" in payload:
            value = payload.get("jitter_seconds")
            if value in ("", None):
                data["jitter_seconds"] = None
            else:
                try:
                    data["jitter_seconds"] = max(0, int(value))
                except (TypeError, ValueError) as exc:
                    raise ApiError("随机延迟必须是整数秒") from exc
        if "timezone" in payload:
            value = (payload.get("timezone") or "").strip()
            if value:
                try:
                    ZoneInfo(value)
                except Exception as exc:  # noqa: BLE001
                    raise ApiError(f"未知时区: {value}") from exc
            data["timezone"] = value or None
        if "enabled" in payload:
            data["enabled"] = bool(payload.get("enabled"))
        if "note" in payload:
            data["note"] = (payload.get("note") or "").strip() or None
        if "user_agent" in payload:
            data["user_agent"] = (payload.get("user_agent") or "").strip() or None
        if "extra_headers" in payload and isinstance(payload["extra_headers"], dict):
            data["extra_headers"] = payload["extra_headers"]

        if for_create:
            data.setdefault("enabled", True)
            if not data.get("base_url"):
                raise ApiError("缺少站点地址 base_url")
            if not data.get("name"):
                uid = ""
                if data.get("cookie"):
                    from ..client import parse_cookie_string

                    uid = parse_cookie_string(data["cookie"]).get("c_secure_uid", "")[:8]
                data["name"] = f"{data.get('site', 'pt').upper()}-{uid or 'account'}"
            if not data.get("cookie"):
                raise ApiError("缺少 Cookie，可通过 curl 粘贴导入")
        return data


def _mask(cookie: str) -> str:
    from ..store import _mask_cookie

    return _mask_cookie(cookie)


class Handler(BaseHTTPRequestHandler):
    server_version = "PTCheckin/1.0"
    protocol_version = "HTTP/1.1"

    api: Api
    settings: Any

    # ------------------------------------------------------------- plumbing
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        token = (self.settings.get("web") or {}).get("auth_token") or ""
        if not token:
            return True
        provided = self.headers.get("X-Auth-Token", "")
        if not provided:
            provided = (query.get("token") or [""])[0]
        if not provided:
            raw = self.headers.get("Cookie", "")
            for part in raw.split(";"):
                k, _, v = part.strip().partition("=")
                if k == "ptcheckin_token":
                    provided = v
                    break
        return provided == token

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._send_json({"error": "文件不存在"}, 404)
            return
        data = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if path.suffix == ".js":
            ctype = "application/javascript; charset=utf-8"
        elif path.suffix in (".html", ".css"):
            ctype = f"{ctype}; charset=utf-8" if "charset" not in ctype else ctype
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(f"请求体不是合法 JSON: {exc}") from exc
        return data if isinstance(data, dict) else {"value": data}

    # --------------------------------------------------------------- routes
    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)

        try:
            if not self._authorized(query):
                self._send_json({"error": "未授权：请提供正确的访问令牌", "code": "unauthorized"}, 401)
                return

            if path.startswith("/api/"):
                self._send_json(self._route_api(method, path, query))
                return

            if path in ("/", "/index.html"):
                self._send_file(STATIC_DIR / "index.html")
                return

            if path.startswith("/static/"):
                rel = path[len("/static/") :]
                target = (STATIC_DIR / rel).resolve()
                if not str(target).startswith(str(STATIC_DIR.resolve())):
                    self._send_json({"error": "非法路径"}, 400)
                    return
                self._send_file(target)
                return

            self._send_json({"error": "接口不存在"}, 404)
        except ApiError as exc:
            self._send_json({"error": exc.message}, exc.status)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.exception("处理请求失败 %s %s", method, path)
            self._send_json({"error": f"服务器内部错误: {exc}"}, 500)

    def _route_api(self, method: str, path: str, query: dict[str, list[str]]) -> Any:
        api = self.api
        body = self._read_json() if method in ("POST", "PUT", "DELETE") else {}

        if path == "/api/health":
            return {"ok": True, "time": iso(), "version": _version()}

        if path == "/api/overview" and method == "GET":
            return api.overview()
        if path == "/api/stats" and method == "GET":
            today = utcnow().astimezone(api._tz()).date().isoformat()  # noqa: SLF001
            return {"stats": api.store.stats(today)}

        if path == "/api/accounts":
            if method == "GET":
                return api.list_accounts()
            if method == "POST":
                return api.create_account(body)

        m = re.fullmatch(r"/api/accounts/(\d+)", path)
        if m:
            account_id = int(m.group(1))
            if method == "GET":
                return api.get_account(account_id)
            if method in ("PUT", "PATCH"):
                return api.update_account(account_id, body)
            if method == "DELETE":
                return api.delete_account(account_id)

        m = re.fullmatch(r"/api/accounts/(\d+)/(checkin|refresh|test)", path)
        if m and method == "POST":
            account_id = int(m.group(1))
            trigger = {"checkin": "manual", "test": "manual", "refresh": "refresh"}[m.group(2)]
            return api.checkin(account_id, trigger=trigger)

        m = re.fullmatch(r"/api/accounts/(\d+)/calendar", path)
        if m and method == "GET":
            month = (query.get("month") or [""])[0]
            return api.calendar(int(m.group(1)), month)

        m = re.fullmatch(r"/api/accounts/(\d+)/records", path)
        if m and method == "GET":
            return api.records(
                int(m.group(1)),
                status=(query.get("status") or [""])[0],
                date_from=(query.get("date_from") or [""])[0],
                date_to=(query.get("date_to") or [""])[0],
                page=_int(query, "page", 1),
                page_size=_int(query, "page_size", 20),
            )

        m = re.fullmatch(r"/api/accounts/(\d+)/days", path)
        if m and method == "GET":
            return api.days(
                int(m.group(1)),
                (query.get("date_from") or [""])[0] or None,
                (query.get("date_to") or [""])[0] or None,
            )

        if path == "/api/records" and method == "GET":
            account_id = (query.get("account_id") or [""])[0]
            return api.records(
                int(account_id) if account_id.isdigit() else None,
                status=(query.get("status") or [""])[0],
                date_from=(query.get("date_from") or [""])[0],
                date_to=(query.get("date_to") or [""])[0],
                page=_int(query, "page", 1),
                page_size=_int(query, "page_size", 20),
            )

        if path == "/api/checkin-all" and method == "POST":
            return api.checkin_all(trigger="manual")

        if path == "/api/settings":
            if method == "GET":
                return api.get_settings()
            if method == "PUT":
                return api.update_settings(body)

        if path == "/api/curl/preview" and method == "POST":
            return api.curl_preview(body)

        if path == "/api/notify/test" and method == "POST":
            return api.test_notify()

        if path == "/api/scheduler" and method == "GET":
            return {"scheduler": api.scheduler.status(), "plans": api.scheduler.pending_plans()}
        if path == "/api/scheduler/tick" and method == "POST":
            return api.scheduler_tick()

        if path == "/api/maintenance/purge" and method == "POST":
            return api.purge(body.get("keep_days"))

        raise ApiError(f"未知接口: {method} {path}", 404)


def _int(query: dict[str, list[str]], key: str, default: int) -> int:
    try:
        return int((query.get(key) or [str(default)])[0])
    except (TypeError, ValueError):
        return default


def _version() -> str:
    from .. import __version__

    return __version__


def create_server(
    settings, store: Store, service: CheckinService, scheduler: Scheduler, host: str | None = None,
    port: int | None = None,
) -> ThreadingHTTPServer:
    api = Api(store, settings, service, scheduler)
    web_cfg = settings.get("web") or {}
    bind_host = host or web_cfg.get("host") or "127.0.0.1"
    bind_port = int(port or web_cfg.get("port") or 8787)

    handler = type(
        "BoundHandler",
        (Handler,),
        {"api": api, "settings": settings},
    )
    httpd = ThreadingHTTPServer((bind_host, bind_port), handler)
    httpd.daemon_threads = True
    log.info("Web 服务已启动: http://%s:%s", bind_host, bind_port)
    return httpd


def serve_forever(httpd: ThreadingHTTPServer) -> None:
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("收到中断信号，正在退出…")
    finally:
        httpd.server_close()

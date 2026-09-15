"""签到结果通知：支持通用 Webhook / Server酱 / Bark / Telegram / ntfy。

所有通知失败都不会影响签到主流程，仅记录日志并返回结果，便于在界面上排查。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .logutil import get_logger

log = get_logger("notify")

EVENT_LABELS = {
    "success": "签到成功",
    "already": "今日已签到",
    "failure": "签到失败",
    "recovered": "签到已恢复",
    "test": "测试通知",
}

STATUS_LABELS = {
    "success": "✅ 签到成功",
    "already": "☑️ 今日已签到",
    "failed": "❌ 签到失败",
    "auth_failed": "🔑 Cookie 失效",
    "network_error": "🌐 网络错误",
}


@dataclass
class NotifyResult:
    channel: str
    name: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"channel": self.channel, "name": self.name, "ok": self.ok, "detail": self.detail}


class Notifier:
    def __init__(self, settings):
        self.settings = settings

    # ------------------------------------------------------------ channel
    def config(self) -> dict[str, Any]:
        return self.settings.get("notify") or {}

    def channels(self, enabled_only: bool = True) -> list[dict[str, Any]]:
        items = self.config().get("channels") or []
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            if enabled_only and item.get("enabled") is False:
                continue
            if item.get("type"):
                out.append(item)
        return out

    def should_send(self, event: str) -> bool:
        cfg = self.config()
        if event == "test":
            return True
        if not cfg.get("enabled"):
            return False
        mapping = {
            "success": "on_success",
            "already": "on_already",
            "failure": "on_failure",
            "recovered": "on_recovered",
        }
        key = mapping.get(event)
        return bool(cfg.get(key, True)) if key else True

    # ------------------------------------------------------------ message
    @staticmethod
    def format_checkin(result: Any, account: dict[str, Any]) -> tuple[str, str]:
        """根据签到结果生成标题与正文。"""
        name = account.get("name") or f"账号{account.get('id')}"
        head = STATUS_LABELS.get(getattr(result, "status", ""), getattr(result, "status", ""))
        title = f"[PT签到] {name} {head}"

        lines = [f"**账号**：{name}", f"**站点**：{account.get('base_url', '')}"]
        check_date = getattr(result, "site_date", None) or getattr(result, "check_date", None)
        if check_date:
            lines.append(f"**日期**：{check_date}")

        fields = [
            ("本次获得", getattr(result, "points", None), " 憨豆"),
            ("连续签到", getattr(result, "streak", None), " 天"),
            ("累计签到", getattr(result, "total_count", None), " 次"),
        ]
        for label, value, unit in fields:
            if value is not None:
                lines.append(f"**{label}**：{value}{unit}")

        rank, rank_total = getattr(result, "rank", None), getattr(result, "rank_total", None)
        if rank is not None:
            lines.append(f"**今日排名**：{rank} / {rank_total}")

        message = getattr(result, "message", None)
        if message:
            lines.append(f"**站点提示**：{message}")
        error = getattr(result, "error", None)
        if error:
            lines.append(f"**错误**：{error}")
        http_status = getattr(result, "http_status", None)
        if http_status:
            lines.append(f"**HTTP**：{http_status}（{getattr(result, 'duration_ms', 0)} ms）")
        trigger = getattr(result, "trigger", None)
        if trigger:
            lines.append(f"**触发方式**：{trigger}")
        return title, "\n".join(lines)

    # --------------------------------------------------------------- send
    def send(self, title: str, body: str, event: str = "test") -> list[NotifyResult]:
        results: list[NotifyResult] = []
        if not self.should_send(event):
            return results
        for channel in self.channels():
            ctype = str(channel.get("type", "")).lower()
            name = channel.get("name") or ctype
            try:
                detail = self._dispatch(ctype, channel, title, body)
                results.append(NotifyResult(ctype, name, True, detail))
                log.info("通知已发送 [%s/%s]", ctype, name)
            except Exception as exc:  # noqa: BLE001 - 通知失败不能影响签到
                log.warning("通知发送失败 [%s/%s]: %s", ctype, name, exc)
                results.append(NotifyResult(ctype, name, False, str(exc)))
        return results

    def notify_checkin(self, result: Any, account: dict[str, Any]) -> list[NotifyResult]:
        event = {
            "success": "success",
            "already": "already",
        }.get(getattr(result, "status", ""), "failure")
        if event == "success" and getattr(result, "recovered", False):
            event = "recovered"
        title, body = self.format_checkin(result, account)
        return self.send(title, body, event)

    # ---------------------------------------------------------- dispatch
    def _dispatch(self, ctype: str, channel: dict[str, Any], title: str, body: str) -> str:
        if ctype in ("webhook", "generic", "json"):
            url = channel.get("url") or ""
            if not url:
                raise ValueError("缺少 url")
            payload = {
                "title": title,
                "body": body,
                "text": f"{title}\n{body}",
                "event": channel.get("event", ""),
                "source": "pt-checkin",
            }
            return _post_json(url, payload, timeout=15)

        if ctype in ("serverchan", "server_chan", "sct"):
            sendkey = channel.get("sendkey") or channel.get("key") or ""
            if not sendkey:
                raise ValueError("缺少 sendkey")
            url = channel.get("url") or f"https://sctapi.ftqq.com/{sendkey}.send"
            return _post_form(url, {"title": title[:100], "desp": body}, timeout=15)

        if ctype == "bark":
            base = (channel.get("url") or "https://api.day.app").rstrip("/")
            key = channel.get("key") or channel.get("device_key") or ""
            url = f"{base}/{key}" if key else base
            return _post_json(
                url,
                {"title": title, "body": body, "group": channel.get("group", "PT签到")},
                timeout=15,
            )

        if ctype == "telegram":
            token = channel.get("bot_token") or channel.get("token") or ""
            chat_id = channel.get("chat_id") or ""
            if not token or not chat_id:
                raise ValueError("缺少 bot_token / chat_id")
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            return _post_json(
                url,
                {"chat_id": chat_id, "text": f"{title}\n\n{body}", "parse_mode": "Markdown"},
                timeout=15,
            )

        if ctype == "ntfy":
            url = channel.get("url") or ""
            if not url:
                raise ValueError("缺少 url")
            req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
            req.add_header("Title", _ascii_header(title))
            req.add_header("Tags", "white_check_mark" if "成功" in title else "warning")
            if channel.get("token"):
                req.add_header("Authorization", f"Bearer {channel['token']}")
            return _open(req, 15)

        raise ValueError(f"不支持的通知类型: {ctype}")


# ------------------------------------------------------------------ helpers
def _ascii_header(text: str) -> str:
    """HTTP 头只允许 latin-1；中文标题做 URL 编码以兼容 ntfy 等。"""
    try:
        text.encode("latin-1")
        return text
    except UnicodeEncodeError:
        return urllib.parse.quote(text)


def _open(req: urllib.request.Request, timeout: float) -> str:
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as resp:
            payload = resp.read(2048).decode("utf-8", errors="replace")
            if int(getattr(resp, "status", 200) or 200) >= 400:
                raise RuntimeError(f"HTTP {resp.status}: {payload[:200]}")
            return payload[:300]
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(300).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def _post_json(url: str, payload: dict[str, Any], timeout: float = 15) -> str:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    return _open(req, timeout)


def _post_form(url: str, payload: dict[str, Any], timeout: float = 15) -> str:
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    return _open(req, timeout)

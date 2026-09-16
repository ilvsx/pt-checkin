"""签到结果通知：支持通用 Webhook / Server酱 / Bark / Telegram / ntfy。

所有通知失败都不会影响签到主流程，仅记录日志并返回结果，便于在界面上排查。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
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

    # 与 settings.DEFAULTS 保持一致：默认只在成功/失败/恢复时通知，"已签到"不打扰
    DEFAULT_EVENTS = {"success": True, "already": False, "failure": True, "recovered": True}

    def should_send(self, event: str) -> bool:
        cfg = self.config()
        if event == "test":
            return True
        if not cfg.get("enabled"):
            return False
        key = {
            "success": "on_success",
            "already": "on_already",
            "failure": "on_failure",
            "recovered": "on_recovered",
        }.get(event)
        if key is None:
            return True
        return bool(cfg.get(key, self.DEFAULT_EVENTS.get(event, True)))

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

        if ctype in ("feishu", "lark", "feishu_webhook"):
            return self._send_feishu_webhook(channel, title, body)

        if ctype in ("feishu_app", "lark_app", "feishu_bot"):
            return self._send_feishu_app(channel, title, body)

        raise ValueError(f"不支持的通知类型: {ctype}")

    # ---------------------------------------------------------------- 飞书
    def _send_feishu_webhook(self, channel: dict[str, Any], title: str, body: str) -> str:
        """飞书自定义机器人 Webhook。

        注意：飞书在**业务失败时依然返回 HTTP 200**（如签名错误、关键词不匹配），
        真正的结果在响应体的 ``code`` 字段里，所以必须解析响应体。
        """
        url = (channel.get("url") or channel.get("webhook") or "").strip()
        if not url:
            raise ValueError("缺少 url（飞书自定义机器人 Webhook 地址）")
        payload = build_feishu_webhook_payload(title, body, use_card=bool(channel.get("card")))
        secret = (channel.get("secret") or "").strip()
        if secret:
            timestamp, sign = feishu_sign(secret)
            # timestamp / sign 必须与 msg_type 同级
            payload["timestamp"] = timestamp
            payload["sign"] = sign
        raw = _post_json(url, payload, timeout=15)
        _raise_on_feishu_error(raw)
        return raw

    def _send_feishu_app(self, channel: dict[str, Any], title: str, body: str) -> str:
        """飞书应用机器人：先用 app_id/app_secret 换 token，再按 receive_id 发消息。"""
        app_id = (channel.get("app_id") or "").strip()
        app_secret = (channel.get("app_secret") or "").strip()
        receive_id = (channel.get("receive_id") or channel.get("chat_id") or "").strip()
        receive_id_type = (channel.get("receive_id_type") or "chat_id").strip()
        if not app_id or not app_secret:
            raise ValueError("缺少 app_id / app_secret")
        if not receive_id:
            raise ValueError("缺少 receive_id（群 chat_id / open_id / email）")

        token_url = channel.get("token_url") or (
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        )
        raw = _post_json(token_url, {"app_id": app_id, "app_secret": app_secret}, timeout=15)
        _raise_on_feishu_error(raw, action="获取 tenant_access_token")
        try:
            token = json.loads(raw).get("tenant_access_token")
        except (json.JSONDecodeError, AttributeError) as exc:
            raise RuntimeError(f"解析 tenant_access_token 响应失败: {raw[:120]}") from exc
        if not token:
            raise RuntimeError("飞书未返回 tenant_access_token")

        send_url = (channel.get("send_url") or "https://open.feishu.cn/open-apis/im/v1/messages") + (
            "?receive_id_type=" + urllib.parse.quote(receive_id_type)
        )
        payload = build_feishu_app_payload(
            receive_id, title, body, use_card=bool(channel.get("card"))
        )
        raw = _post_json(
            send_url, payload, timeout=15, headers={"Authorization": f"Bearer {token}"}
        )
        _raise_on_feishu_error(raw, action="发送消息")
        return raw


# ------------------------------------------------------------------ helpers
# ------------------------------------------------------------------ 飞书
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def feishu_sign(secret: str, timestamp: int | str | None = None) -> tuple[str, str]:
    """飞书自定义机器人的「加签」。

    算法（见飞书文档）：把 ``"{timestamp}\\n{secret}"`` 作为 HMAC-SHA256 的 **key**，
    消息体为空，结果做 Base64。注意 key 与 msg 的位置容易写反。

    Returns:
        ``(timestamp, sign)``
    """
    ts = str(timestamp if timestamp is not None else int(time.time()))
    string_to_sign = f"{ts}\n{secret}"
    digest = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    return ts, base64.b64encode(digest).decode("utf-8")


def _strip_md(text: str) -> str:
    """飞书纯文本消息不渲染 Markdown，去掉 ** 以免显示成字面量。"""
    return _MD_BOLD_RE.sub(r"\1", text or "")


def feishu_card(title: str, body: str) -> dict[str, Any]:
    """构造飞书消息卡片（支持 lark_md，**加粗** 可正常渲染）。"""
    text = f"{title}\n{body}"
    if "失败" in title or "❌" in text or "失效" in title or "错误" in title:
        color = "red"
    elif "已签到" in title or "☑" in text:
        color = "grey"
    elif "测试" in title:
        color = "blue"
    else:
        color = "green"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": color,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": body}}],
    }


def build_feishu_webhook_payload(title: str, body: str, use_card: bool = False) -> dict[str, Any]:
    """自定义机器人 Webhook 的请求体（``content`` 是对象，``card`` 单独一层）。"""
    if use_card:
        return {"msg_type": "interactive", "card": feishu_card(title, body)}
    return {"msg_type": "text", "content": {"text": f"{title}\n{_strip_md(body)}"}}


def build_feishu_app_payload(
    receive_id: str, title: str, body: str, use_card: bool = False
) -> dict[str, Any]:
    """应用机器人 im/v1 的请求体（``content`` 必须是 **JSON 字符串**）。"""
    if use_card:
        msg_type, content = "interactive", feishu_card(title, body)
    else:
        msg_type, content = "text", {"text": f"{title}\n{_strip_md(body)}"}
    return {
        "receive_id": receive_id,
        "msg_type": msg_type,
        "content": json.dumps(content, ensure_ascii=False),
    }


def _raise_on_feishu_error(raw: str, action: str = "发送消息") -> None:
    """飞书业务失败也返回 HTTP 200，必须检查响应体里的 ``code``。"""
    text = (raw or "").strip()
    if not text:
        return
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return
    code = data.get("code")
    if code in (None, 0, "0"):
        return
    detail = data.get("msg") or data.get("StatusMessage") or ""
    raise RuntimeError(f"{action}失败 code={code}: {detail}")


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
            payload = resp.read(4096).decode("utf-8", errors="replace")
            if int(getattr(resp, "status", 200) or 200) >= 400:
                raise RuntimeError(f"HTTP {resp.status}: {payload[:200]}")
            # 不在这里截断：飞书等平台需要解析响应体中的业务状态码
            return payload
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(300).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def _post_json(
    url: str, payload: dict[str, Any], timeout: float = 15, headers: dict[str, str] | None = None
) -> str:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    return _open(req, timeout)


def _post_form(url: str, payload: dict[str, Any], timeout: float = 15) -> str:
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    return _open(req, timeout)

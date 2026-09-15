"""站点页面解析。

HHClub（NexusPHP + 定制签到页）的 ``attendance.php`` 会在 HTML 内嵌两部分
结构化数据，我们直接解析它们，而不是用脆弱的文本匹配：

1. ``const nowDate = new Date("2026/09/15")``  —— 站点认定的"今天"
2. ``let data = {"2026-09-15":{...}, ...}``    —— 按日期索引的签到台账

因此"当日是否已签到"的判断是确定性的：``nowDate 格式化后的日期`` 是否存在于
台账中。文本消息（第 N 次签到 / 连续 M 天 / 获得 P 憨豆 / 排名）作为补充信息
与降级兜底。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# 站点未登录时返回的页面很小且不含任何结构化数据
MIN_AUTH_PAGE_BYTES = 20_000

_NOW_DATE_RE = re.compile(r"""nowDate\s*=\s*new\s+Date\(\s*["']([^"']+)["']\s*\)""")
_LEDGER_START_RE = re.compile(r'\{\s*"(\d{4}-\d{2}-\d{2})"\s*:\s*\{')
_DATE_DISPLAY_RE = re.compile(r'id="date-display"[^>]*>\s*([0-9]{4}-[0-9]{2})', re.S)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)

_MESSAGE_RE = re.compile(
    r"这是您的第\s*(\d+)\s*次签到[，,]\s*已连续签到\s*(\d+)\s*天[，,]\s*"
    r"本次签到获得\s*(\d+)\s*个憨豆[。.]?\s*你目前拥有补签卡\s*(\d+)\s*张"
)
_RANK_RE = re.compile(r"今日签到排名[：:]\s*(\d+)\s*/\s*(\d+)")
_MSG_TOTAL_RE = re.compile(r"第\s*(\d+)\s*次签到")
_MSG_STREAK_RE = re.compile(r"已连续签到\s*(\d+)\s*天")
_MSG_POINTS_RE = re.compile(r"本次签到获得\s*(\d+)\s*个(?:憨豆|积分|魔力值)")
_MSG_CARDS_RE = re.compile(r"补签卡\s*(\d+)\s*张")

_LOGIN_HINTS = (
    "takelogin",
    'name="username"',
    'name="password"',
    "你还没有登录",
    "请先登录",
    "登录后才能",
)
_BANNED_HINTS = ("账号被禁用", "你的账号已被禁用", "帐号被禁用", "Account disabled", "已被封禁")


@dataclass
class AttendanceRecord:
    """站点台账中的一条签到记录。"""

    date: str
    points: int | None = None
    raw_id: int | None = None
    uid: int | None = None
    is_retroactive: int = 0
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "points": self.points,
            "id": self.raw_id,
            "uid": self.uid,
            "is_retroactive": self.is_retroactive,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class AttendancePage:
    """一次 attendance.php 响应的解析结果。"""

    authenticated: bool = False
    site_date: str | None = None
    site_date_source: str = "none"  # page|ledger|fallback|none
    records: dict[str, AttendanceRecord] = field(default_factory=dict)
    message: str | None = None
    total_count: int | None = None
    streak: int | None = None
    points: int | None = None
    retro_cards: int | None = None
    rank: int | None = None
    rank_total: int | None = None
    error: str | None = None
    title: str | None = None

    @property
    def today_record(self) -> AttendanceRecord | None:
        if self.site_date:
            return self.records.get(self.site_date)
        return None

    @property
    def signed_today(self) -> bool:
        """当日是否已签到（结构化数据优先，文本消息兜底）。"""
        if self.today_record is not None:
            return True
        # 兜底：页面本身就是"今日签到结果"页
        if self.message and ("本次签到获得" in self.message or "已连续签到" in self.message):
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "authenticated": self.authenticated,
            "site_date": self.site_date,
            "site_date_source": self.site_date_source,
            "signed_today": self.signed_today,
            "record_count": len(self.records),
            "message": self.message,
            "total_count": self.total_count,
            "streak": self.streak,
            "points": self.points,
            "retro_cards": self.retro_cards,
            "rank": self.rank,
            "rank_total": self.rank_total,
            "error": self.error,
            "title": self.title,
        }


# --------------------------------------------------------------------- utils
def match_brace(text: str, start: int) -> int:
    """从 ``text[start]``（必须是 ``{``）开始做括号配对，返回匹配的 ``}`` 下标。

    正确处理字符串字面量与转义，避免把字符串里的花括号算进深度。
    """
    if start >= len(text) or text[start] != "{":
        return -1
    depth = 0
    in_string = False
    escaped = False
    i = start
    while i < len(text):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1


def normalize_date(value: str | None) -> str | None:
    """把 ``2026/09/15``、``2026-9-5`` 等统一成 ``2026-09-15``。"""
    if not value:
        return None
    m = re.match(r"\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", value)
    if not m:
        return None
    year, month, day = m.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def extract_now_date(html: str) -> str | None:
    m = _NOW_DATE_RE.search(html)
    return normalize_date(m.group(1)) if m else None


def extract_ledger(html: str) -> dict[str, AttendanceRecord]:
    """提取按日期索引的签到台账；失败返回空字典。"""
    for m in _LEDGER_START_RE.finditer(html):
        end = match_brace(html, m.start())
        if end == -1:
            continue
        blob = html[m.start() : end + 1]
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or not obj:
            continue
        records: dict[str, AttendanceRecord] = {}
        ok = True
        for key, value in obj.items():
            if not isinstance(value, dict) or "date" not in value:
                ok = False
                break
            date = normalize_date(str(value.get("date") or key)) or key
            records[date] = AttendanceRecord(
                date=date,
                points=_as_int(value.get("points")),
                raw_id=_as_int(value.get("id")),
                uid=_as_int(value.get("uid")),
                is_retroactive=_as_int(value.get("is_retroactive")) or 0,
                created_at=_as_str(value.get("created_at")),
                updated_at=_as_str(value.get("updated_at")),
            )
        if ok:
            return records
    return {}


def extract_message(html: str) -> tuple[str | None, dict[str, int | None]]:
    """提取"本次签到"提示语及其中的数字。"""
    info: dict[str, int | None] = {
        "total_count": None,
        "streak": None,
        "points": None,
        "retro_cards": None,
        "rank": None,
        "rank_total": None,
    }
    message: str | None = None

    m = _MESSAGE_RE.search(html)
    if m:
        info["total_count"] = int(m.group(1))
        info["streak"] = int(m.group(2))
        info["points"] = int(m.group(3))
        info["retro_cards"] = int(m.group(4))
        message = m.group(0)
    else:
        # 站点文案变动时逐项兜底
        for key, pattern in (
            ("total_count", _MSG_TOTAL_RE),
            ("streak", _MSG_STREAK_RE),
            ("points", _MSG_POINTS_RE),
            ("retro_cards", _MSG_CARDS_RE),
        ):
            mm = pattern.search(html)
            if mm:
                info[key] = int(mm.group(1))
        # 抓取包含"签到"的提示段落原文，便于排查
        mm = re.search(r'<p[^>]*class="[^"]*register-info[^"]*"[^>]*>(.*?)</p>', html, re.S)
        if mm:
            message = re.sub(r"<[^>]+>", "", mm.group(1)).strip() or None

    rm = _RANK_RE.search(html)
    if rm:
        info["rank"] = int(rm.group(1))
        info["rank_total"] = int(rm.group(2))
    return message, info


# --------------------------------------------------------------------- main
def parse_attendance(html: str, fallback_date: str | None = None) -> AttendancePage:
    """解析 attendance.php 的响应。

    Args:
        html: 响应正文
        fallback_date: 页面未给出 nowDate 时使用的日期（通常为账号时区的今天）
    """
    page = AttendancePage()
    if not html:
        page.error = "响应为空"
        return page

    title = _TITLE_RE.search(html)
    page.title = re.sub(r"\s+", " ", title.group(1)).strip() if title else None

    page.site_date = extract_now_date(html)
    if page.site_date:
        page.site_date_source = "page"

    page.records = extract_ledger(html)
    if page.site_date is None and page.records:
        page.site_date = max(page.records)
        page.site_date_source = "ledger"
    if page.site_date is None and _DATE_DISPLAY_RE.search(html) and fallback_date:
        page.site_date = fallback_date
        page.site_date_source = "fallback"

    has_structured = bool(page.site_date) or "register-now-info" in html
    page.authenticated = has_structured

    if page.authenticated:
        message, info = extract_message(html)
        page.message = message
        page.total_count = info["total_count"]
        page.streak = info["streak"]
        page.points = info["points"]
        page.retro_cards = info["retro_cards"]
        page.rank = info["rank"]
        page.rank_total = info["rank_total"]
        if not page.records and fallback_date:
            # 台账缺失时仍能给出日期（消息兜底判断已签到）
            page.site_date = page.site_date or fallback_date
    else:
        page.error = _diagnose_unauthenticated(html)

    return page


def _diagnose_unauthenticated(html: str) -> str:
    for hint in _BANNED_HINTS:
        if hint in html:
            return "账号可能已被禁用"
    for hint in _LOGIN_HINTS:
        if hint in html:
            return "登录状态失效，Cookie 无效或已过期"
    if len(html) < MIN_AUTH_PAGE_BYTES:
        return "登录状态失效（页面被截断，未包含签到数据），请更新 Cookie"
    return "未在页面中找到签到数据，站点结构可能已变更"


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)

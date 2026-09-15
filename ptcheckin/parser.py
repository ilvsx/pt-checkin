"""站点页面解析。

支持的签到台账格式（自动识别）：

1. **对象台账**（HHClub）—— 页面内嵌按日期索引的 JSON::

       const nowDate = new Date("2026/09/15");
       let data = {"2026-09-15":{"points":125,"created_at":"2026-09-15 11:07:51",...}, ...}

2. **日历事件台账**（HDFans 及同类 NexusPHP 站点）—— FullCalendar 事件数组::

       let events = JSON.parse('[{"start":"2026-09-15","display":"background"},
                                {"start":"2026-09-15","title":60}, ...]');

   有背景事件的日期即"已签到"，``title`` 为当天获得的积分。

两种格式都能给出**日期级别**的确定性判断：``站点今天`` 是否在台账里。
另有站点提示文本（第 N 次签到 / 连续 M 天 / 获得 P 积分 / 排名）作为补充信息与降级兜底。
"""

from __future__ import annotations

import html as _html
import json
import re
from dataclasses import dataclass, field
from typing import Any

# 站点未登录时返回的页面很小且不含任何结构化数据（HHClub 截断页特征）
MIN_AUTH_PAGE_BYTES = 20_000

_NOW_DATE_RE = re.compile(r"""nowDate\s*=\s*new\s+Date\(\s*["']([^"']+)["']\s*\)""")
_OBJECT_LEDGER_RE = re.compile(r'\{\s*"(\d{4}-\d{2}-\d{2})"\s*:\s*\{')
_EVENT_LEDGER_RE = re.compile(r"JSON\.parse\(\s*'((?:[^'\\]|\\.)*)'\s*\)")
_RAW_EVENT_ARRAY_RE = re.compile(r'\[\s*\{\s*"start"\s*:')
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)

# 站点提示：本次签到获得 P 个<单位>。<中间可能夹着补签说明>你目前拥有补签卡 K 张。
_MESSAGE_RE = re.compile(
    r"这是您的第\s*(\d+)\s*次签到[，,]\s*已连续签到\s*(\d+)\s*天[，,]\s*"
    r"本次签到获得\s*(\d+)\s*个\s*([^\s。.，,0-9]*)"
)
_CARDS_TAIL_RE = re.compile(r".{0,160}?你目前拥有补签卡\s*(\d+)\s*张[。.]?", re.S)
_RANK_RE = re.compile(r"今日签到排名[：:]\s*(\d+)\s*/\s*(\d+)")
# 顶部用户栏：[签到已得60, 补签卡: 5]
_SIGNIN_BONUS_RE = re.compile(r"签到已得\s*([\d,]+)")

_LOGIN_HINTS = (
    "takelogin",
    'name="username"',
    "name='username'",
    "你还没有登录",
    "请先登录",
    "登录后才能",
)
_BANNED_HINTS = ("账号被禁用", "你的账号已被禁用", "帐号被禁用", "Account disabled", "已被封禁")
_LOGIN_TITLE_HINT = "登录"


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
    """一次签到页响应的解析结果。"""

    authenticated: bool = False
    site_date: str | None = None
    site_date_source: str = "none"  # page|ledger|fallback|none
    ledger_format: str = "none"  # object|events|none
    records: dict[str, AttendanceRecord] = field(default_factory=dict)
    message: str | None = None
    total_count: int | None = None
    streak: int | None = None
    points: int | None = None
    points_unit: str | None = None
    retro_cards: int | None = None
    rank: int | None = None
    rank_total: int | None = None
    signin_bonus: int | None = None
    error: str | None = None
    title: str | None = None

    @property
    def today_record(self) -> AttendanceRecord | None:
        if self.site_date:
            return self.records.get(self.site_date)
        return None

    @property
    def signed_today(self) -> bool:
        """当日是否已签到。

        台账是权威依据，只有**完全没有台账**时才退回文本判断。
        反过来（有台账却忽略它、改用文本）会把"解析不到台账"误判成"已签到"，
        从而让调度器整天不再签到 —— 这是代价最大的失败模式。
        """
        if not self.site_date:
            return False
        if self.records:
            return self.site_date in self.records
        # 兜底：页面本身就是"今日签到结果"页，且完全拿不到台账
        if self.message and ("本次签到获得" in self.message or "已连续签到" in self.message):
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "authenticated": self.authenticated,
            "site_date": self.site_date,
            "site_date_source": self.site_date_source,
            "ledger_format": self.ledger_format,
            "signed_today": self.signed_today,
            "record_count": len(self.records),
            "message": self.message,
            "total_count": self.total_count,
            "streak": self.streak,
            "points": self.points,
            "points_unit": self.points_unit,
            "retro_cards": self.retro_cards,
            "rank": self.rank,
            "rank_total": self.rank_total,
            "signin_bonus": self.signin_bonus,
            "error": self.error,
            "title": self.title,
        }


# --------------------------------------------------------------------- utils
_PAIRS = {"{": "}", "[": "]"}


def match_balanced(text: str, start: int) -> int:
    """从 ``text[start]``（``{`` 或 ``[``）开始配对，返回匹配的闭合下标。

    正确处理字符串字面量与转义，忽略字符串内部的括号。
    """
    if start >= len(text) or text[start] not in _PAIRS:
        return -1
    stack: list[str] = []
    in_string = False
    quote = ""
    escaped = False
    i = start
    while i < len(text):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_string = False
        else:
            if ch in "\"'":
                in_string = True
                quote = ch
            elif ch in _PAIRS:
                stack.append(_PAIRS[ch])
            elif ch in "}]":
                if not stack or stack[-1] != ch:
                    return -1
                stack.pop()
                if not stack:
                    return i
        i += 1
    return -1


def match_brace(text: str, start: int) -> int:
    """``match_balanced`` 的别名，保留以兼容既有调用。"""
    return match_balanced(text, start)


def normalize_date(value: str | None) -> str | None:
    """把 ``2026/09/15``、``2026-9-5`` 等统一成 ``2026-09-15``。"""
    if not value:
        return None
    m = re.match(r"\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", str(value))
    if not m:
        return None
    year, month, day = m.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def html_to_text(html: str) -> str:
    """把页面转成便于正则的纯文本：去掉脚本/样式/标签，折叠空白。"""
    text = re.sub(r"<script\b.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = text.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


def extract_now_date(html: str) -> str | None:
    m = _NOW_DATE_RE.search(html)
    return normalize_date(m.group(1)) if m else None


# ------------------------------------------------------------------- ledgers
def _extract_object_ledger(html: str) -> dict[str, AttendanceRecord]:
    """HHClub 形态：``let data = {"YYYY-MM-DD": {...}}``。"""
    for m in _OBJECT_LEDGER_RE.finditer(html):
        end = match_balanced(html, m.start())
        if end == -1:
            continue
        try:
            obj = json.loads(html[m.start() : end + 1])
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


def _parse_events_array(payload: str) -> dict[str, AttendanceRecord]:
    """把 FullCalendar 事件数组转成 ``{日期: 记录}``。

    **只有带 ``title``（积分）的日期才算已签到。**

    日历里同时存在两类事件：``{"display":"background"}`` 与 ``{"title":60}``。
    起初以为"有背景事件即已签到"，但实测被证伪：

    * QingWa 的日历标记了 **31 天**（2026-08-16 → 09-15），其中只有 1 天带积分，
      而站点自报 **「已连续签到 1 天」** —— 若背景事件也算签到，连续天数应 ≥ 31。
    * HDFans 同样吻合：标记 61 天、其中 57 天带积分，连续 6 天即最近 6 个带积分日期。

    所以 ``display:"background"`` 只是标记"该日在补签窗口内"（页面上那句
    "点击白色背景的圆点进行补签"正是指这些日子），**并不代表已签到**。
    若把未签到的今天当成已签到，调度器将永远不会签到，因此这里必须严格。
    """
    try:
        events = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    if not isinstance(events, list) or not events:
        return {}

    records: dict[str, AttendanceRecord] = {}
    for item in events:
        if not isinstance(item, dict) or "start" not in item:
            continue
        if item.get("title") is None:
            # 背景事件：仅表示在补签窗口内，不是签到记录
            continue
        date = normalize_date(item.get("start"))
        if not date:
            continue
        points = _as_int(item.get("title"))
        existing = records.get(date)
        if existing is None or points is not None:
            records[date] = AttendanceRecord(
                date=date, points=points, is_retroactive=0
            )
    return records


def extract_calendar_events(html: str) -> dict[str, AttendanceRecord]:
    """HDFans 形态：``let events = JSON.parse('[...]')``。"""
    for m in _EVENT_LEDGER_RE.finditer(html):
        records = _parse_events_array(_unescape_js_single_quoted(m.group(1)))
        if records:
            return records

    # 兜底：直接内联的 JSON 数组（未经 JSON.parse 包装）
    for m in _RAW_EVENT_ARRAY_RE.finditer(html):
        end = match_balanced(html, m.start())
        if end == -1:
            continue
        records = _parse_events_array(html[m.start() : end + 1])
        if records:
            return records
    return {}


def _unescape_js_single_quoted(text: str) -> str:
    """还原 JS 单引号字符串里的转义序列。"""
    return text.replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")


def extract_ledger(html: str) -> tuple[dict[str, AttendanceRecord], str]:
    """自动识别台账格式。返回 ``(记录字典, 格式名)``。"""
    records = _extract_object_ledger(html)
    if records:
        return records, "object"
    records = extract_calendar_events(html)
    if records:
        return records, "events"
    return {}, "none"


# ------------------------------------------------------------------ messages
def extract_message(html: str) -> tuple[str | None, dict[str, Any]]:
    """从页面纯文本中提取签到提示及其中的数字。"""
    info: dict[str, Any] = {
        "total_count": None,
        "streak": None,
        "points": None,
        "points_unit": None,
        "retro_cards": None,
        "rank": None,
        "rank_total": None,
    }
    plain = html_to_text(html)
    message: str | None = None

    m = _MESSAGE_RE.search(plain)
    if m:
        info["total_count"] = int(m.group(1))
        info["streak"] = int(m.group(2))
        info["points"] = int(m.group(3))
        info["points_unit"] = (m.group(4) or "").strip() or None

        # 把后续的"你目前拥有补签卡 K 张"一并纳入提示语，中间可能夹着补签说明
        end = m.end()
        cards = _CARDS_TAIL_RE.match(plain[m.end() : m.end() + 200])
        if cards:
            info["retro_cards"] = int(cards.group(1))
            end = m.end() + cards.end()
        message = plain[m.start() : end].strip()

    # 兜底：逐项抓取
    if info["total_count"] is None:
        mm = re.search(r"第\s*(\d+)\s*次签到", plain)
        if mm:
            info["total_count"] = int(mm.group(1))
    if info["streak"] is None:
        mm = re.search(r"已连续签到\s*(\d+)\s*天", plain)
        if mm:
            info["streak"] = int(mm.group(1))
    if info["points"] is None:
        mm = re.search(r"本次签到获得\s*(\d+)\s*个\s*([^\s。.，,0-9]*)", plain)
        if mm:
            info["points"] = int(mm.group(1))
            info["points_unit"] = (mm.group(2) or "").strip() or None
    if info["retro_cards"] is None:
        mm = re.search(r"补签卡\s*[:：]?\s*(\d+)\s*张", plain)
        if mm:
            info["retro_cards"] = int(mm.group(1))
    if message is None:
        mm = re.search(r"签到(?:成功|失败|已签到)[^。]{0,140}。", plain)
        if mm:
            message = mm.group(0).strip()

    rm = _RANK_RE.search(plain)
    if rm:
        info["rank"] = int(rm.group(1))
        info["rank_total"] = int(rm.group(2))
    return message, info


def extract_signin_bonus(html: str) -> tuple[int | None, int | None]:
    """解析顶部用户栏的 ``[签到已得60, 补签卡: 5]``。

    注意：该数值的语义（"今日已得"还是"最近一次已得"）无法百分百确认，
    因此**只作为展示信息**，不参与"今日是否已签到"的判定。
    """
    plain = html_to_text(html)
    m = re.search(r"签到已得\s*([\d,]+)\s*,\s*补签卡\s*[:：]\s*(\d+)", plain)
    if m:
        return _as_int(m.group(1).replace(",", "")), _as_int(m.group(2))
    m2 = _SIGNIN_BONUS_RE.search(plain)
    return (_as_int(m2.group(1).replace(",", "")) if m2 else None), None


# --------------------------------------------------------------------- main
def parse_attendance(
    html: str, fallback_date: str | None = None, site: str | None = None
) -> AttendancePage:
    """解析签到页响应。

    Args:
        html: 响应正文
        fallback_date: 页面未给出站点日期时使用的日期（通常为账号时区的今天）
        site: 站点 key（解析本身自动识别台账格式，此参数仅作备注）
    """
    page = AttendancePage()
    if not html:
        page.error = "响应为空"
        return page

    title = _TITLE_RE.search(html)
    page.title = re.sub(r"\s+", " ", title.group(1)).strip() if title else None

    # 1) 站点日期。优先级很重要：
    #    a. 页面自报的 nowDate（HHClub 提供，最权威）
    #    b. 账号时区的今天（HDFans 等不提供 nowDate 的站点）
    #    c. 台账里最大的日期 —— 仅当前两者都拿不到时才用。
    #       绝不能优先用它：若今天尚未签到，max(records) 会是"上次签到的日期"，
    #       于是 site_date 指向过去，signed_today 就会误判为已签到，导致永远不签到。
    page.site_date = extract_now_date(html)
    if page.site_date:
        page.site_date_source = "page"
    page.records, page.ledger_format = extract_ledger(html)

    # 只有"页面自报日期"或"真实台账"才算已登录的证据。
    # 必须在套用 fallback_date 之前算出来，否则任意页面都会因兜底日期而被当成已登录。
    strong_evidence = bool(page.records) or page.site_date is not None

    if page.site_date is None and fallback_date:
        page.site_date = fallback_date
        page.site_date_source = "fallback"
    if page.site_date is None and page.records:
        page.site_date = max(page.records)
        page.site_date_source = "ledger"

    # 2) 登录状态：结构化数据最可信，其次是登录页特征
    login_hint = any(hint in html for hint in _LOGIN_HINTS)

    if strong_evidence and not login_hint:
        page.authenticated = True
    elif login_hint:
        page.error = _diagnose_unauthenticated(html)
    else:
        # 无结构化数据时看文本证据（提示语 / 用户栏）
        plain = html_to_text(html)
        text_evidence = (
            "register-now-info" in html
            or "签到成功" in plain
            or "签到已得" in plain
            or "次签到" in plain
        )
        page.authenticated = text_evidence
        if not text_evidence:
            page.error = _diagnose_unauthenticated(html)

    if not page.authenticated:
        return page

    # 3) 文本提示与排名
    message, info = extract_message(html)
    page.message = message
    page.total_count = info["total_count"]
    page.streak = info["streak"]
    page.points = info["points"]
    page.points_unit = info["points_unit"]
    page.retro_cards = info["retro_cards"]
    page.rank = info["rank"]
    page.rank_total = info["rank_total"]

    bonus, cards = extract_signin_bonus(html)
    page.signin_bonus = bonus
    if page.retro_cards is None:
        page.retro_cards = cards

    return page


def _diagnose_unauthenticated(html: str) -> str:
    for hint in _BANNED_HINTS:
        if hint in html:
            return "账号可能已被禁用"
    for hint in _LOGIN_HINTS:
        if hint in html:
            return "登录状态失效，Cookie 无效或已过期"
    title = _TITLE_RE.search(html)
    if title and _LOGIN_TITLE_HINT in _html.unescape(title.group(1)):
        return "登录状态失效（被重定向到登录页），请更新 Cookie"
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

"""站点档案（site profile）。

不同 PT 站虽然同为 NexusPHP，但签到页的实现细节不同：

================  ==========================  ================================
站点              签到台账格式                 签到请求
================  ==========================  ================================
HHClub            ``let data = {"日期": {...}}``   GET attendance.php（幂等）
HDFans            ``let events = JSON.parse(...)`` GET attendance.php（幂等）
================  ==========================  ================================

解析器本身对台账格式做自动识别（见 ``parser.extract_ledger``），
这里主要承载"请求方式"和"展示文案"这类站点差异。
"""

from __future__ import annotations

import urllib.parse
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SiteProfile:
    key: str
    name: str
    default_base_url: str
    # 签到接口路径（相对站点根目录）
    attendance_path: str = "attendance.php"
    # 签到请求的 Referer（相对站点根目录）
    referer: str = "index.php"
    # 积分单位，仅用于界面与通知展示
    points_unit: str = "积分"
    # 备注：说明该站的台账形态，便于排查
    note: str = ""


SITES: dict[str, SiteProfile] = {
    "hhanclub": SiteProfile(
        key="hhanclub",
        name="HHClub",
        default_base_url="https://hhanclub.net",
        attendance_path="attendance.php",
        referer="mybonus.php",
        points_unit="憨豆",
        note="台账内嵌为 let data = {\"YYYY-MM-DD\": {...}}；Cookie 失效时返回 200 截断页",
    ),
    "hdfans": SiteProfile(
        key="hdfans",
        name="HDFans",
        default_base_url="https://hdfans.org",
        attendance_path="attendance.php",
        referer="index.php",
        points_unit="魔力值",
        note="台账内嵌为 FullCalendar events JSON；Cookie 失效时 302 跳转 login.php",
    ),
    "qingwapt": SiteProfile(
        key="qingwapt",
        name="QingWa",
        default_base_url="https://www.qingwapt.com",
        attendance_path="attendance.php",
        referer="index.php",
        points_unit="蝌蚪",
        note="会话 Cookie 为单一的 qw_session（非 c_secure_* 形态）；台账同为 FullCalendar events；Cookie 失效时 302 跳转 login.php",
    ),
}

DEFAULT_SITE = "hhanclub"

# 主机名别名 → 站点 key
_HOST_ALIASES: tuple[tuple[str, str], ...] = (
    ("hhan", "hhanclub"),
    ("hdfan", "hdfans"),
    ("qingwa", "qingwapt"),
    ("frog", "qingwapt"),
)


def get_profile(key: str | None) -> SiteProfile:
    """按 key 取站点档案；未知 key 回退到默认站点。"""
    normalized = (key or "").strip().lower()
    if normalized in SITES:
        return SITES[normalized]
    return SITES[DEFAULT_SITE]


def list_profiles() -> list[dict]:
    return [asdict(p) for p in SITES.values()]


def guess_site(base_url: str) -> str:
    """根据站点地址猜测站点 key；无法判断时返回主机名。"""
    host = urllib.parse.urlparse(base_url or "").netloc.lower()
    if not host:
        return DEFAULT_SITE
    bare = host.split(":")[0]
    for key in SITES:
        if key in bare:
            return key
    for alias, key in _HOST_ALIASES:
        if alias in bare:
            return key
    return bare or DEFAULT_SITE


def is_known_site(key: str | None) -> bool:
    return (key or "").strip().lower() in SITES

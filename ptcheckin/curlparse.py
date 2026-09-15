"""解析浏览器"Copy as cURL"文本，快速导入站点地址与 Cookie。"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any


def shell_split(text: str) -> list[str]:
    """把一段 shell 命令拆成参数列表，支持单/双引号与反斜杠转义。

    仅实现解析所需的子集，不执行任何命令。
    """
    text = text.replace("\r\n", "\n").replace("\\\n", " ").replace("\n", " ")
    tokens: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    has_token = False
    i = 0
    while i < len(text):
        ch = text[i]
        if quote == "'":
            if ch == "'":
                quote = None
            else:
                buf.append(ch)
        elif quote == '"':
            if ch == "\\" and i + 1 < len(text) and text[i + 1] in '"\\$`':
                buf.append(text[i + 1])
                i += 1
            elif ch == '"':
                quote = None
            else:
                buf.append(ch)
        else:
            if ch in ("'", '"'):
                quote = ch
                has_token = True
            elif ch == "\\" and i + 1 < len(text):
                buf.append(text[i + 1])
                i += 1
                has_token = True
            elif ch.isspace():
                if buf or has_token:
                    tokens.append("".join(buf))
                    buf = []
                    has_token = False
            else:
                buf.append(ch)
                has_token = True
        i += 1
    if buf or has_token:
        tokens.append("".join(buf))
    return tokens


def _guess_site(base_url: str) -> str:
    """按主机名识别站点（见 sites.py 的站点档案）。"""
    from .sites import guess_site

    return guess_site(base_url)


def parse_curl(text: str) -> dict[str, Any]:
    """从 curl 命令中提取请求信息。

    Returns:
        dict 包含 url / base_url / site / method / cookie / user_agent /
        referer / headers / warnings
    """
    raw = (text or "").strip()
    result: dict[str, Any] = {
        "url": "",
        "base_url": "",
        "site": "",
        "method": "GET",
        "cookie": "",
        "user_agent": "",
        "referer": "",
        "headers": {},
        "warnings": [],
    }
    if not raw:
        result["warnings"].append("输入为空")
        return result

    tokens = shell_split(raw)
    if not tokens:
        result["warnings"].append("无法解析输入内容")
        return result

    # 去掉开头的 curl / curl.exe
    if tokens and re.fullmatch(r"curl(\.exe)?", tokens[0], re.I):
        tokens = tokens[1:]
    else:
        result["warnings"].append("输入看起来不是 curl 命令，已尽力解析")

    def take_value(idx: int) -> tuple[str | None, int]:
        if idx + 1 < len(tokens):
            return tokens[idx + 1], idx + 1
        return None, idx

    i = 0
    cookie_parts: list[str] = []
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-H", "--header") or tok.startswith("--header="):
            if tok.startswith("--header="):
                value = tok.split("=", 1)[1]
            else:
                value, i = take_value(i)
            if value and ":" in value:
                key, _, val = value.partition(":")
                key = key.strip()
                val = val.strip()
                lk = key.lower()
                if lk == "cookie":
                    cookie_parts.append(val)
                elif lk == "user-agent":
                    result["user_agent"] = val
                elif lk == "referer":
                    result["referer"] = val
                else:
                    result["headers"][key] = val
        elif tok in ("-b", "--cookie") or tok.startswith("--cookie="):
            if tok.startswith("--cookie="):
                value = tok.split("=", 1)[1]
            else:
                value, i = take_value(i)
            if value:
                cookie_parts.append(value)
        elif tok in ("-A", "--user-agent") or tok.startswith("--user-agent="):
            if tok.startswith("--user-agent="):
                value = tok.split("=", 1)[1]
            else:
                value, i = take_value(i)
            if value:
                result["user_agent"] = value
        elif tok in ("-e", "--referer") or tok.startswith("--referer="):
            if tok.startswith("--referer="):
                value = tok.split("=", 1)[1]
            else:
                value, i = take_value(i)
            if value:
                result["referer"] = value
        elif tok in ("-X", "--request"):
            value, i = take_value(i)
            if value:
                result["method"] = value.upper()
        elif tok in ("--data", "--data-raw", "--data-binary", "-d") or tok.startswith("--data="):
            value, i = take_value(i) if not tok.startswith("--data=") else (tok.split("=", 1)[1], i)
            if value:
                result["method"] = "POST" if result["method"] == "GET" else result["method"]
                result["_data"] = value
        elif tok.startswith("http://") or tok.startswith("https://"):
            result["url"] = tok
        i += 1

    if not result["url"]:
        result["warnings"].append("未找到请求 URL")

    if cookie_parts:
        # 合并多段 Cookie 并去重（后者优先）
        jar: dict[str, str] = {}
        for part in cookie_parts:
            for item in part.split(";"):
                item = item.strip()
                if item and "=" in item:
                    k, _, v = item.partition("=")
                    jar[k.strip()] = v.strip()
        result["cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())

    if result["url"]:
        parsed = urllib.parse.urlparse(result["url"])
        result["base_url"] = f"{parsed.scheme}://{parsed.netloc}"
        result["site"] = _guess_site(result["base_url"])
        if not result["referer"]:
            result["referer"] = result["url"]

    if not result["cookie"]:
        result["warnings"].append("未在 curl 中找到 Cookie，请确认复制时包含了 -b 或 Cookie 头")

    return result

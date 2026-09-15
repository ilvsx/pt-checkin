"""HTTP 客户端（仅使用标准库）。

刻意不引入 requests/httpx，方便在 NAS、路由器等环境零依赖运行。
"""

from __future__ import annotations

import gzip
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any

from .logutil import get_logger

log = get_logger("client")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

REQUIRED_COOKIE_KEYS = ("c_secure_uid", "c_secure_pass")


class SiteError(Exception):
    """网络/协议层错误。"""


@dataclass
class HttpResponse:
    status: int
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    elapsed_ms: int = 0
    error: str | None = None

    @property
    def text(self) -> str:
        return decode_body(self.body, self.headers.get("content-type", ""))

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)


def decode_body(body: bytes, content_type: str = "") -> str:
    charset = ""
    if "charset=" in content_type:
        charset = content_type.split("charset=", 1)[1].split(";")[0].strip().strip('"\'')
    for enc in (charset, "utf-8", "gb18030", "latin-1"):
        if not enc:
            continue
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


def _decompress(body: bytes, encoding: str) -> bytes:
    encoding = (encoding or "").lower()
    if not body:
        return body
    try:
        if "gzip" in encoding:
            return gzip.decompress(body)
        if "deflate" in encoding:
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
    except Exception as exc:  # noqa: BLE001 - 解压失败时回退原始数据
        log.warning("响应解压失败(%s): %s", encoding, exc)
    return body


def parse_cookie_string(cookie: str) -> dict[str, str]:
    """把 ``a=1; b=2`` 形式的 Cookie 解析成字典。"""
    out: dict[str, str] = {}
    for part in (cookie or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        out[key.strip()] = value.strip()
    return out


def cookie_string_from_dict(data: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in data.items() if v)


def validate_cookie(cookie: str) -> tuple[bool, str]:
    """校验 Cookie 是否像一份有效的 NexusPHP 会话。"""
    cookie = (cookie or "").strip()
    if not cookie:
        return False, "Cookie 为空"
    jar = parse_cookie_string(cookie)
    missing = [k for k in REQUIRED_COOKIE_KEYS if not jar.get(k)]
    if missing:
        return False, f"缺少必要的 Cookie 字段: {', '.join(missing)}"
    if "c_secure_login" in jar and jar["c_secure_login"].lower() not in ("bm9wzq%3d%3d", "bm9wzq=="):
        # bm9wZQ%3D%3D / bm9wZQ== 都是 "nope"（正常值），其余值可能是错误状态
        pass
    return True, "Cookie 格式正常"


class SiteClient:
    """针对单个 PT 站点的轻量 HTTP 客户端。"""

    def __init__(
        self,
        base_url: str,
        cookie: str,
        *,
        user_agent: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: float = 30,
        verify_ssl: bool = True,
        proxy: str = "",
        attendance_path: str = "attendance.php",
        referer: str = "index.php",
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.cookie = (cookie or "").strip()
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.extra_headers = {k: v for k, v in (extra_headers or {}).items() if v}
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.proxy = proxy
        # 站点差异：签到接口路径与 Referer（见 sites.py 的站点档案）
        self.attendance_path = attendance_path or "attendance.php"
        self.referer = referer or "index.php"
        self._opener = self._build_opener()

    # ------------------------------------------------------------- opener
    def _build_opener(self) -> urllib.request.OpenerDirector:
        handlers: list[Any] = []
        if not self.verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener(*handlers)

    def _headers(self, referer: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8,"
                "application/signed-exchange;v=b3;q=0.7"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Cookie": self.cookie,
            "Upgrade-Insecure-Requests": "1",
            "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin",
            "sec-fetch-user": "?1",
            "priority": "u=0, i",
            "Connection": "keep-alive",
        }
        if referer:
            headers["Referer"] = referer
        headers.update(self.extra_headers)
        return headers

    # ----------------------------------------------------------- requests
    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        data: dict[str, str] | None = None,
        referer: str | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        body = urllib.parse.urlencode(data).encode("utf-8") if data else None
        req = urllib.request.Request(url, data=body, method=method)
        for key, value in self._headers(referer).items():
            req.add_header(key, value)
        if body is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")

        started = time.monotonic()
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                headers = {k.lower(): v for k, v in resp.headers.items()}
                raw = _decompress(raw, headers.get("content-encoding", ""))
                return HttpResponse(
                    status=int(getattr(resp, "status", resp.getcode()) or 0),
                    url=resp.geturl(),
                    headers=headers,
                    body=raw,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
        except urllib.error.HTTPError as exc:
            raw = b""
            try:
                raw = exc.read()
            except Exception:  # noqa: BLE001
                pass
            headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
            raw = _decompress(raw, headers.get("content-encoding", ""))
            return HttpResponse(
                status=int(exc.code),
                url=url,
                headers=headers,
                body=raw,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=f"HTTP {exc.code} {exc.reason}",
            )
        except urllib.error.URLError as exc:
            return HttpResponse(
                status=0,
                url=url,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=f"网络错误: {exc.reason}",
            )
        except ssl.SSLError as exc:
            return HttpResponse(
                status=0, url=url, elapsed_ms=int((time.monotonic() - started) * 1000),
                error=f"TLS 错误: {exc}",
            )
        except Exception as exc:  # noqa: BLE001
            return HttpResponse(
                status=0, url=url, elapsed_ms=int((time.monotonic() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )

    # ------------------------------------------------------------ helpers
    def attendance(self, timeout: float | None = None) -> HttpResponse:
        """访问签到接口。

        注意：在 HHClub 与 HDFans 上，该 GET 请求本身就是签到动作，
        且对同一天幂等 —— 已签到时会原样返回当日结果，不会重复签到。
        """
        return self.request(
            self.attendance_path,
            referer=f"{self.base_url}/{self.referer.lstrip('/')}",
            timeout=timeout,
        )

    def mybonus(self, timeout: float | None = None) -> HttpResponse:
        return self.request("mybonus.php", referer=f"{self.base_url}/index.php", timeout=timeout)

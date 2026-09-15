"""Web 层：标准库 http.server 实现的 JSON API + 静态页面。"""

from __future__ import annotations

from .server import create_server, serve_forever

__all__ = ["create_server", "serve_forever"]

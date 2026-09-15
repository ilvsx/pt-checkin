"""日志工具：同时输出到控制台与轮转文件。"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

_CONFIGURED = False


def setup_logging(log_dir: Path | None = None, level: str = "INFO", debug: bool = False) -> logging.Logger:
    """初始化根 logger（幂等）。"""
    global _CONFIGURED
    root = logging.getLogger("ptcheckin")
    if _CONFIGURED:
        return root

    root.setLevel(logging.DEBUG if debug else getattr(logging, level.upper(), logging.INFO))
    root.propagate = False
    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_dir / "ptcheckin.log",
                maxBytes=2 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as exc:  # 只读文件系统等场景下不致命
            root.warning("无法创建日志文件 %s: %s", log_dir, exc)

    _CONFIGURED = True
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"ptcheckin.{name}")

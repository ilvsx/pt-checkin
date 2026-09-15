#!/usr/bin/env python3
"""PT 自动签到系统入口。

用法::

    python3 run.py                 # 启动 Web 界面 + 定时调度
    python3 run.py once            # 立即签到一次
    python3 run.py status          # 查看今日状态
    python3 run.py add --curl '...'# 添加账号
    python3 run.py --help
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ptcheckin.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

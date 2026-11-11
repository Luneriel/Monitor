"""Monitor 追踪计时程序 —— 启动入口。

    python main.py            # 正常启动
    python main.py --silent   # 静默启动（仅托盘图标）
    python main.py --selftest --shots data/shots --db data/selftest.db   # 自检（必须指定 --db）
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from monitor.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

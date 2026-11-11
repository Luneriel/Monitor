"""PyInstaller 运行期钩子：窗口模式下给标准流兜底。

用 ``--windowed`` 打包出来的程序没有控制台，PyInstaller 会把 ``sys.stdout``、
``sys.stderr``、``sys.stdin`` 全部设成 ``None``。这时程序里任何一句 ``print()``
（例如日志页删完日志后的提示、``MONITOR_TRACE`` 的追踪输出）都会抛
``AttributeError: 'NoneType' object has no attribute 'write'`` 直接崩掉，
第三方库（Qt、warnings）往 stderr 写东西也一样。

这里把三个流换成 ``os.devnull``：效果等同于「有控制台时把输出重定向到 NUL」。
有控制台时（开发运行、``--console`` 打包）本钩子什么都不做，不影响任何正常路径。
"""

import os
import sys

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")
if sys.stdin is None:
    sys.stdin = open(os.devnull, "r", encoding="utf-8", errors="replace")

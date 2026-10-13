"""Process-wide runtime flags.

Kept in a leaf module so widgets split out of main.py can read them without
importing main (which would be a circular import).
"""

from __future__ import annotations

import sys

# 调试模式：`python src/main.py --debug` 打开（run_debug.bat 会带上），
# 用于在托盘菜单里保留「测试表情/裸参数」等开发用入口。
DEBUG_MODE = "--debug" in sys.argv

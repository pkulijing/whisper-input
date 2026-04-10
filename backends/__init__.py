"""平台后端抽象层 - 运行时按 sys.platform 选择对应实现。"""

import sys

IS_LINUX = sys.platform == "linux"
IS_MACOS = sys.platform == "darwin"

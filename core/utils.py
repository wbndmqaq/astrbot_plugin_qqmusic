"""跨模块通用小工具。

本模块只依赖标准库、**不导入**插件内任何其它模块，因此可被 service / resolve /
delivery / handlers 任意一侧安全复用，不会制造 ``service ↔ resolve`` 循环导入
（service 反向委托 resolve，resolve 不能再回头 import service）。
"""

from __future__ import annotations


def safe_int(v, default: int = 0) -> int:
    """容错整数转换：None / 非数字字符串等一律回退 ``default``。

    配置项与 API 返回字段的类型不可控（可能是 str/None/float），原先 service 与
    resolve 各有一份完全相同的 try/int/except 实现，现统一收敛到这里。
    """
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

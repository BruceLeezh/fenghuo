"""静默兜底的留痕器。

背景：设计上很多路径必须"降级而不是崩溃"——一个数据源挂了不该让整份评分失败，
一条缓存读不出来不该让页面 500。但 `except Exception: pass` 的问题是
**失败连痕迹都没有**：线上出问题时，日志里干净得像什么都没发生过，只能靠猜。

本模块提供一个极小的调用：`log_swallow("模块:行号")`。
* 第一次在某位置遇到某类异常 → WARNING（能进默认日志，可被监控采到）
* 同类同位置再次出现 → DEBUG（循环里每行都刷 WARNING 会把真正的异常淹掉）
* 计数常驻内存，供自检/压测时读 `swallowed_snapshot()` 确认降级是否真的发生

它只负责"记录发生过什么"，不改变控制流：调用完照旧 pass / continue。
"""
import logging
import sys
from collections import Counter

__all__ = ["log_swallow", "swallowed_snapshot", "reset_swallowed"]

_logger = logging.getLogger("fenghuo.swallowed")
_seen = Counter()          # (where, exc_type) → 次数


def log_swallow(where: str) -> None:
    exc = sys.exc_info()[1]
    kind = type(exc).__name__ if exc is not None else "None"
    key = (where, kind)
    _seen[key] += 1
    if _seen[key] == 1:
        _logger.warning("已降级兜底：%s（%s: %s）", where, kind, exc)
    else:
        _logger.debug("已降级兜底（第 %d 次）：%s（%s）", _seen[key], where, kind)


def swallowed_snapshot() -> dict:
    """当前进程内的兜底计数：{"where|ExcType": n}。"""
    return {"%s|%s" % k: v for k, v in _seen.items()}


def reset_swallowed() -> None:
    _seen.clear()

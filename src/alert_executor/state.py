"""执行器内存态：for_rounds 防抖计数 + 飞书 RateLimiter。

纪律（设计文档 §6 / §12 拒绝项）：防抖状态放进程内存，重启归零——计数丢失的后果是
漏报几轮（方向安全：告警宁漏勿误），不引入 Redis。
"""

from __future__ import annotations

import time


class Debounce:
    """for_rounds：连续 M 轮达标才放行 firing；未达标清零（静默跳过语义）。"""

    def __init__(self) -> None:
        self._hits: dict[str, int] = {}

    def record(self, rule_code: str, hit: bool, for_rounds: int) -> bool:
        """记录一轮判定结果，返回是否达到放行条件（连续 for_rounds 轮达标）。"""
        if not hit:
            self._hits.pop(rule_code, None)
            return False
        hits = self._hits.get(rule_code, 0) + 1
        self._hits[rule_code] = hits
        return hits >= for_rounds

    def reset(self, rule_code: str) -> None:
        self._hits.pop(rule_code, None)

    def hits(self, rule_code: str) -> int:
        """当前连续达标轮数（仅观测用）。"""
        return self._hits.get(rule_code, 0)


class RateLimiter:
    """per-key 最小发送间隔（秒）；interval <= 0 恒放行（对齐现状每轮发）。"""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}

    def allow(self, key: str, interval_seconds: float,
              now: float | None = None) -> bool:
        if interval_seconds <= 0:
            return True
        now = time.monotonic() if now is None else now
        last = self._last.get(key)
        if last is not None and now - last < interval_seconds:
            return False
        self._last[key] = now
        return True

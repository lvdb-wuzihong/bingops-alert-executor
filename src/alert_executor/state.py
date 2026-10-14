"""执行器内存态：for_rounds 防抖计数 + 飞书 RateLimiter + 触发序列追踪。

纪律（设计文档 §6 / §12 拒绝项）：防抖/序列状态放进程内存，重启归零——丢失的后果是
漏报几轮恢复通知/防抖重计（方向安全：告警宁漏勿误），不引入 Redis。
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


class SequenceTracker:
    """per-rule 触发序列追踪（PromQL vector 场景，内存态）：diff 本轮/上轮序列集合。

    序列身份 = metric labels（sorted 后拼接）。三类差异：
    - added：本轮新增的触发序列 → 拆分发独立告警卡；
    - ongoing：上轮也在触发 → 持续告警（同样拆分直发，节奏=评估间隔）；
    - recovered：上轮触发、本轮消失 → 发「已恢复」绿卡（部分恢复也能感知到具体序列）。
    重启归零：首轮无上轮参照，全部视为新增（宁漏勿误方向安全）。
    """

    def __init__(self) -> None:
        self._last: dict[str, dict[str, dict]] = {}

    @staticmethod
    def seq_key(sample: dict) -> str:
        """序列身份：metric labels（排序后拼接）；无 labels 时用整个样本。"""
        metric = sample.get("metric") or {}
        if not metric:
            return repr(sorted(sample.items()))
        return "|".join(f"{k}={v}" for k, v in sorted(metric.items()))

    def diff(self, rule_code: str,
             cur_samples: list[dict]) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict]]:
        """记录本轮并返回 (added, ongoing, recovered)，value 均为样本本身。"""
        cur = {self.seq_key(s): s for s in cur_samples if isinstance(s, dict)}
        prev = self._last.get(rule_code, {})
        self._last[rule_code] = cur
        added = {k: s for k, s in cur.items() if k not in prev}
        ongoing = {k: s for k, s in cur.items() if k in prev}
        recovered = {k: s for k, s in prev.items() if k not in cur}
        return added, ongoing, recovered

    def clear(self, rule_code: str) -> None:
        self._last.pop(rule_code, None)

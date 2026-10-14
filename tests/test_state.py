"""防抖/限流/序列追踪状态（内存态，红线 6）。"""

from __future__ import annotations

from alert_executor.state import Debounce, RateLimiter, SequenceTracker


class TestDebounce:
    def test_for_rounds_one_fires_immediately(self):
        d = Debounce()
        assert d.record("r", True, 1) is True

    def test_requires_consecutive_hits(self):
        d = Debounce()
        assert d.record("r", True, 3) is False
        assert d.record("r", True, 3) is False
        assert d.record("r", True, 3) is True

    def test_miss_resets_counter(self):
        d = Debounce()
        d.record("r", True, 3)
        d.record("r", True, 3)
        assert d.record("r", False, 3) is False
        assert d.hits("r") == 0
        assert d.record("r", True, 3) is False

    def test_rules_are_isolated(self):
        d = Debounce()
        d.record("a", True, 2)
        assert d.record("b", True, 2) is False


class TestRateLimiter:
    def test_zero_interval_always_allows(self):
        rl = RateLimiter()
        assert rl.allow("k", 0, now=1000) is True
        assert rl.allow("k", 0, now=1000.5) is True

    def test_blocks_within_interval(self):
        rl = RateLimiter()
        assert rl.allow("k", 60, now=1000) is True
        assert rl.allow("k", 60, now=1030) is False

    def test_allows_after_interval(self):
        rl = RateLimiter()
        assert rl.allow("k", 60, now=1000) is True
        assert rl.allow("k", 60, now=1060) is True

    def test_keys_are_isolated(self):
        rl = RateLimiter()
        assert rl.allow("a", 60, now=1000) is True
        assert rl.allow("b", 60, now=1001) is True


class TestSequenceTracker:
    def test_first_round_all_added(self):
        t = SequenceTracker()
        samples = [{"metric": {"instance": "a"}, "value": "1"},
                   {"metric": {"instance": "b"}, "value": "2"}]
        added, ongoing, recovered = t.diff("r", samples)
        assert set(added) == {"instance=a", "instance=b"}
        assert ongoing == {} and recovered == {}

    def test_ongoing_and_recovered(self):
        t = SequenceTracker()
        a = {"metric": {"instance": "a"}, "value": "1"}
        b = {"metric": {"instance": "b"}, "value": "2"}
        t.diff("r", [a, b])
        b2 = {"metric": {"instance": "b"}, "value": "3"}  # b 持续（值更新）
        added, ongoing, recovered = t.diff("r", [b2])
        assert added == {}
        assert set(ongoing) == {"instance=b"}
        assert set(recovered) == {"instance=a"}
        assert recovered["instance=a"] is a  # 恢复样本带上轮内容（供恢复卡展示）

    def test_seq_key_independent_of_value(self):
        """序列身份只看 labels：同 labels 值变化 = ongoing 而非 added+recovered。"""
        t = SequenceTracker()
        t.diff("r", [{"metric": {"i": "1"}, "value": "10"}])
        added, ongoing, _ = t.diff("r", [{"metric": {"i": "1"}, "value": "20"}])
        assert added == {} and set(ongoing) == {"i=1"}

    def test_clear(self):
        t = SequenceTracker()
        t.diff("r", [{"metric": {"i": "1"}, "value": "1"}])
        t.clear("r")
        added, _, recovered = t.diff("r", [{"metric": {"i": "1"}, "value": "1"}])
        assert set(added) == {"i=1"} and recovered == {}  # 清零后重回新增

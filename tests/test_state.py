"""防抖与限流状态（内存态，红线 6）。"""

from __future__ import annotations

from alert_executor.state import Debounce, RateLimiter


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

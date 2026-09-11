"""节拍循环集成：拉配置→评估→回报、防抖门控、error 两路都走、限流、重叠保护、热更新。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from alert_executor.config import (AppConfig, EvaluationConfig, NotifyConfig,
                                   PlatformConfig)
from alert_executor.fetcher import RemoteConfig
from alert_executor.models import EvaluationResult, STATUS_ERROR, STATUS_OK
from alert_executor.scheduler import AlertExecutor
from conftest import make_rule, make_source

import pytest


class FakeEvaluator:
    def __init__(self, results: dict[str, EvaluationResult]) -> None:
        self.results = results
        self.calls = 0

    async def evaluate(self, rule, source) -> EvaluationResult:
        self.calls += 1
        return self.results[rule.code]


class SpyReporter:
    def __init__(self, response: dict | None = None) -> None:
        # response=None 模拟回报失败（data=None → 主旁路默认发飞书）
        self.response = {"notify": True, "event_id": 1} if response is None else response
        self.calls: list[tuple[str, str]] = []

    async def report(self, rule, status, total, ws, we, details, error=None):
        self.calls.append((rule.code, status))
        return self.response


class SpyNotifier:
    def __init__(self) -> None:
        self.alerts: list[str] = []
        self.errors: list[str] = []
        self.channel_updates: list[list] = []

    def update_channels(self, channels) -> None:
        self.channel_updates.append(list(channels))

    async def send_alert(self, rule, result, ws, we) -> None:
        self.alerts.append(rule.code)

    async def send_error(self, rule, error) -> None:
        self.errors.append(rule.code)


class FakeFetcher:
    """按脚本返回拉取结果；None = 无变化/失败（沿用现有配置）。"""

    def __init__(self, script: list[RemoteConfig | None]) -> None:
        self.script = list(script)
        self.versions: list[str | None] = []

    async def fetch(self, version: str | None) -> RemoteConfig | None:
        self.versions.append(version)
        if self.script:
            return self.script.pop(0)
        return None


def make_cfg() -> AppConfig:
    return AppConfig(
        platform=PlatformConfig(base_url="http://b.internal", token_ref="NO_AUTH"),
        notify=NotifyConfig(default_channel={"type": "feishu-bot",
                                             "secret_ref": "NO_AUTH"}),
        evaluation=EvaluationConfig(timeout_seconds=5, per_source_concurrency=4),
    )


def remote(rules, sources=None, version: str = "v1") -> RemoteConfig:
    return RemoteConfig(version=version, data_sources=sources or [make_source()],
                        rules=rules)


def make_executor(script: list[RemoteConfig | None],
                  results: dict[str, EvaluationResult] | None = None,
                  reporter_response: dict | None = None,
                  ) -> tuple[AlertExecutor, FakeEvaluator, SpyReporter, SpyNotifier,
                             FakeFetcher]:
    executor = AlertExecutor(make_cfg(), client=httpx.AsyncClient())
    evaluator = FakeEvaluator(results or {})
    reporter, notifier = SpyReporter(reporter_response), SpyNotifier()
    fetcher = FakeFetcher(script)
    executor._evaluators = {"clickhouse": evaluator}
    executor._reporter = reporter
    executor._notifier = notifier
    executor._fetcher = fetcher
    return executor, evaluator, reporter, notifier, fetcher


async def one_tick(executor: AlertExecutor) -> None:
    """单轮调度（跳过真实时间等待：next_due 清零 + 解除拉取节流）。"""
    executor._next_due.clear()
    executor._last_fetch = 0.0
    await executor._tick()


HIT = EvaluationResult(status=STATUS_OK, hit=True, total_count=9, details="d")
MISS = EvaluationResult(status=STATUS_OK, hit=False, total_count=0)


@pytest.mark.asyncio
async def test_rules_come_from_platform_not_local():
    """规则从平台拉取：初始空集不评估，拉到配置后下一 tick 生效。"""
    executor, evaluator, reporter, notifier, fetcher = make_executor(
        [remote([make_rule(code="r1")], version="v1")], {"r1": HIT})
    # 第一 tick：拉到配置但本 tick 的 due 收集发生在 apply 之后——
    # _maybe_refresh 在收集前执行，因此本轮即评估
    await one_tick(executor)
    assert fetcher.versions == [None]           # 首拉不带版本
    assert reporter.calls == [("r1", "firing")]  # 拉到即生效
    await executor._client.aclose()


@pytest.mark.asyncio
async def test_fetch_failure_keeps_existing_rules():
    rule = make_rule(code="r1")
    executor, evaluator, reporter, notifier, fetcher = make_executor(
        [remote([rule], version="v1"), None],  # 第二轮拉取失败
        {"r1": HIT})
    await one_tick(executor)
    await one_tick(executor)
    assert fetcher.versions == [None, "v1"]      # 版本协商带着持有版本
    assert reporter.calls == [("r1", "firing"), ("r1", "firing")]  # 沿用旧配置继续评估
    await executor._client.aclose()


@pytest.mark.asyncio
async def test_hot_reload_add_and_remove_rules():
    executor, evaluator, reporter, notifier, fetcher = make_executor(
        [remote([make_rule(code="r1")], version="v1"),
         remote([make_rule(code="r2")], version="v2")],
        {"r1": HIT, "r2": HIT})
    await one_tick(executor)
    assert reporter.calls == [("r1", "firing")]
    await one_tick(executor)
    # v2 只剩 r2：r1 被清理（不再评估），新规则 r2 立即生效
    assert reporter.calls == [("r1", "firing"), ("r2", "firing")]
    assert fetcher.versions == [None, "v1"]
    await executor._client.aclose()


@pytest.mark.asyncio
async def test_notify_enabled_decoupled_from_notification():
    """通知与开单解耦：notify_enabled（仅记录/不开单）不影响飞书通知。"""
    executor, evaluator, reporter, notifier, _ = make_executor(
        [remote([make_rule(notify_enabled=False)])], {"r1": HIT})
    await one_tick(executor)
    assert notifier.alerts == ["r1"]                 # 通知照发（依据=渠道绑定，与开单解耦）
    assert reporter.calls == [("r1", "firing")]
    await executor._client.aclose()


@pytest.mark.asyncio
async def test_platform_notify_false_suppresses_feishu():
    """平台 repeat notify 协议：notify=false（活跃 firing 30min 内重复）→ 跳过飞书，webhook 照报。"""
    executor, evaluator, reporter, notifier, _ = make_executor(
        [remote([make_rule()])], {"r1": HIT},
        reporter_response={"notify": False, "suppress_reason": "firing ongoing"})
    await one_tick(executor)
    assert reporter.calls == [("r1", "firing")]  # webhook 照报
    assert notifier.alerts == []                 # 平台抑制 → 飞书不发
    await executor._client.aclose()


@pytest.mark.asyncio
async def test_report_failure_defaults_to_feishu():
    """回报失败（data=None）→ 默认发飞书：主旁路纪律，宁多勿漏。"""
    executor, evaluator, reporter, notifier, _ = make_executor(
        [remote([make_rule()])], {"r1": HIT}, reporter_response=None)
    await one_tick(executor)
    assert reporter.calls == [("r1", "firing")]
    assert notifier.alerts == ["r1"]
    await executor._client.aclose()


@pytest.mark.asyncio
async def test_error_card_rate_limited_on_repeated_failures():
    """持续评估失败：平台 notify=true 时仍受本地限流兑底（每 15min 一张），回报照发。"""
    rule = make_rule()
    err = EvaluationResult.from_error(RuntimeError("ch down"))
    executor, evaluator, reporter, notifier, _ = make_executor(
        [remote([rule])], {"r1": err})
    rule.eval_interval_seconds = 0
    for _ in range(3):
        await one_tick(executor)
    assert notifier.errors == ["r1"]                       # error 卡片仅首张
    assert reporter.calls == [("r1", "error")] * 3          # error 回报照发
    await executor._client.aclose()


@pytest.mark.asyncio
async def test_error_card_platform_throttle_suppresses():
    """多副本权威判定：平台 error 节流返回 notify=false → 两个副本都不发橙卡，回报照发。"""
    rule = make_rule()
    err = EvaluationResult.from_error(RuntimeError("ch down"))
    executor, evaluator, reporter, notifier, _ = make_executor(
        [remote([rule])], {"r1": err},
        reporter_response={"notify": False, "suppress_reason": "error notify throttled"})
    rule.eval_interval_seconds = 0
    for _ in range(3):
        await one_tick(executor)
    assert notifier.errors == []                           # 平台节流 → 不发
    assert reporter.calls == [("r1", "error")] * 3
    await executor._client.aclose()


@pytest.mark.asyncio
async def test_miss_is_silent():
    executor, evaluator, reporter, notifier, _ = make_executor(
        [remote([make_rule()])], {"r1": MISS})
    await one_tick(executor)
    assert notifier.alerts == []
    assert reporter.calls == []  # 低于阈值静默跳过，永不报 resolved
    await executor._client.aclose()


@pytest.mark.asyncio
async def test_error_goes_both_ways():
    executor, evaluator, reporter, notifier, _ = make_executor(
        [remote([make_rule()])],
        {"r1": EvaluationResult.from_error(RuntimeError("ch down"))})
    await one_tick(executor)
    assert notifier.errors == ["r1"]            # 飞书 error 卡片
    assert reporter.calls == [("r1", "error")]  # webhook error 回报
    await executor._client.aclose()


@pytest.mark.asyncio
async def test_for_rounds_debounce_gate():
    executor, evaluator, reporter, notifier, _ = make_executor(
        [remote([make_rule(for_rounds=3)])], {"r1": HIT})
    await one_tick(executor)
    await one_tick(executor)
    assert reporter.calls == []                  # 前两轮被防抖拦下
    await one_tick(executor)
    assert reporter.calls == [("r1", "firing")]  # 第三轮连续达标放行
    assert notifier.alerts == ["r1"]
    await executor._client.aclose()


@pytest.mark.asyncio
async def test_rate_limit_blocks_feishu_not_webhook():
    executor, evaluator, reporter, notifier, _ = make_executor(
        [remote([make_rule(notify_interval_minutes=60)])], {"r1": HIT})
    await one_tick(executor)
    await one_tick(executor)
    assert reporter.calls == [("r1", "firing"), ("r1", "firing")]  # webhook 照报
    assert notifier.alerts == ["r1"]                               # 飞书被限流
    await executor._client.aclose()


@pytest.mark.asyncio
async def test_heartbeat_written_and_failure_tolerant(tmp_path, monkeypatch):
    """心跳文件每 tick 触碰；不可写路径吞 OSError，不影响主路。"""
    hb = tmp_path / "hb"
    monkeypatch.setattr("alert_executor.scheduler.HEARTBEAT_PATH", str(hb))
    executor, evaluator, reporter, notifier, _ = make_executor(
        [remote([make_rule(code="r1")])], {"r1": MISS})
    executor._write_heartbeat()
    assert hb.exists()
    monkeypatch.setattr("alert_executor.scheduler.HEARTBEAT_PATH",
                        str(tmp_path / "no_dir" / "hb"))
    executor._write_heartbeat()  # 不应抛出
    await executor._client.aclose()


@pytest.mark.asyncio
async def test_in_flight_rule_not_rescheduled_mid_eval():
    rule = make_rule()

    class SlowEvaluator(FakeEvaluator):
        async def evaluate(self, rule, source):
            await asyncio.sleep(0.05)
            return HIT

    executor = AlertExecutor(make_cfg(), client=httpx.AsyncClient())
    executor._evaluators = {"clickhouse": SlowEvaluator({"r1": HIT})}
    executor._reporter, executor._notifier = SpyReporter(), SpyNotifier()
    executor._fetcher = FakeFetcher([remote([rule])])
    rule.eval_interval_seconds = 0  # 每 tick 都到期
    task = asyncio.create_task(executor._tick())
    await asyncio.sleep(0.01)
    assert "r1" in executor._in_flight  # 评估中
    await executor._tick()              # 并发 tick：重叠保护生效，不重复评估
    await task
    assert len(executor._reporter.calls) == 1
    await executor._client.aclose()

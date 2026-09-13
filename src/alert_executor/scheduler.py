"""节拍循环：拉配置 → 评估到期规则 → 回报 + 飞书（§12 三步形态）。

调度语义（设计文档 §12）：
- 规则/数据源**从平台拉取**（用户决策 2026-09-10），版本协商按 ``fetch_interval_seconds``
  节流；规则变更生效延迟 ≤ 一个拉取周期；
- 拉取失败沿用上一份配置继续评估（故障自愈靠下一轮重拉，不中断告警评估）；
- 单实例 Deployment，规则级 ``eval_interval_seconds`` 本地调度：next_due = last_eval + interval；
- 重叠保护：评估中的规则跳过本轮（in-flight 集合，进程内实现）；
- 并发池按数据源分组限流（asyncio.Semaphore）+ 单条评估硬超时（评估器内）；
- 拉取方向恒为执行器→平台，零入站端口。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import httpx

from .config import AppConfig, DataSourceConfig, RuleConfig
from .evaluators import build_evaluators
from .fetcher import AgentConfigFetcher, RemoteConfig
from .feishu import FeishuNotifier
from .lease import RedisLeaseCoordinator
from .models import REPORT_ERROR, REPORT_FIRING, STATUS_ERROR, EvaluationResult
from .reporter import PlatformReporter, current_window
from .state import Debounce, RateLimiter

logger = logging.getLogger(__name__)

TICK_SECONDS = 1.0

# 心跳文件：零入站端口（红线 3），容器/K8s 探针用 exec 检查文件新鲜度代替 HTTP healthz
HEARTBEAT_PATH = os.environ.get("ALERT_EXECUTOR_HEARTBEAT", "/tmp/alert-executor-heartbeat")
# 心跳最大陈旧秒数：超过即视为不健康（> tick 周期的数量级即可）
HEARTBEAT_MAX_AGE_SECONDS = 90
# error 卡片最小发送间隔（秒）：持续评估失败时防刷屏（§12 退化 RateLimiter 同款语义）。
# error 回报不受限流（平台统计需要），只限飞书；env 可调（联调期可临时调小）。
ERROR_NOTIFY_MIN_INTERVAL_SECONDS = float(
    os.environ.get("ALERT_EXECUTOR_ERROR_NOTIFY_INTERVAL_SECONDS", "900"),
)

# ── 多副本协调（2026-09-13 用户决策：Redis 租约，见 lease.py） ──────────────────
# REDIS_URL 注入即启用：每条规则同一时刻恰好一个实例评估（Deployment 任意扩缩容）；
# 未注入 → 全量评估（单副本兼容）；Redis 不可用 → fail-open（宁多勿漏，平台兜底）。


class AlertExecutor:
    def __init__(self, cfg: AppConfig, client: httpx.AsyncClient | None = None,
                 fetcher: AgentConfigFetcher | None = None,
                 coordinator: RedisLeaseCoordinator | None = None) -> None:
        self._cfg = cfg
        self._client = client or httpx.AsyncClient(follow_redirects=True)
        self._owns_client = client is None
        self._reporter = PlatformReporter(self._client, cfg.platform)
        self._notifier = FeishuNotifier(self._client, cfg.notify)
        self._evaluators = build_evaluators(self._client, cfg.evaluation)
        self._fetcher = fetcher or AgentConfigFetcher(self._client, cfg.platform)
        self._debounce = Debounce()
        self._limiter = RateLimiter()
        # 运行时配置状态：初始为空，由首轮拉取填充（拉不到则空转等待平台）
        self._config_version: str | None = None
        self._last_fetch = 0.0
        self._rules: list[RuleConfig] = []
        self._sources: dict[str, DataSourceConfig] = {}
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._in_flight: set[str] = set()
        self._next_due: dict[str, float] = {}
        # 多副本协调（2026-09-13 用户决策：Redis 租约）：REDIS_URL 注入即启用——
        # 每条规则同一时刻恰好一个实例评估（Deployment 任意扩缩容）；
        # 未注入 → 全量评估（单副本兼容）；Redis 不可用 → fail-open（宁多勿漏）。
        redis_url = os.environ.get("REDIS_URL")
        self._coordinator: RedisLeaseCoordinator | None = (
            coordinator if coordinator is not None
            else (RedisLeaseCoordinator(redis_url) if redis_url else None)
        )

    async def run(self) -> None:
        if self._coordinator is not None:
            logger.info("alert-executor 启动：平台 %s（多副本 Redis 租约协调）",
                        self._cfg.platform.base_url)
        else:
            logger.info("alert-executor 启动：平台 %s（未启用协调，全量评估）",
                        self._cfg.platform.base_url)
        try:
            while True:
                await self._tick()
                self._write_heartbeat()
                await asyncio.sleep(TICK_SECONDS)
        finally:
            if self._owns_client:
                await self._client.aclose()

    def _write_heartbeat(self) -> None:
        """每 tick 触碰心跳文件；失败只打日志，绝不影响主路。"""
        try:
            Path(HEARTBEAT_PATH).touch()
        except OSError as e:
            logger.warning("心跳文件写入失败 path=%s: %s", HEARTBEAT_PATH, e)

    async def _tick(self) -> None:
        await self._maybe_refresh()
        now = time.monotonic()
        due = [
            rule for rule in self._rules
            if rule.enabled
            and rule.code not in self._in_flight
            and self._next_due.get(rule.code, 0.0) <= now
        ]
        if not due:
            return
        await asyncio.gather(*(self._evaluate(rule) for rule in due))

    async def _maybe_refresh(self) -> None:
        """按 fetch_interval_seconds 节流拉取平台配置；失败沿用现有配置。"""
        if time.monotonic() - self._last_fetch < self._cfg.platform.fetch_interval_seconds:
            return
        self._last_fetch = time.monotonic()
        remote = await self._fetcher.fetch(self._config_version)
        if remote is not None:
            self._apply_config(remote)

    def _apply_config(self, remote: RemoteConfig) -> None:
        """热更新规则集：删除项清理调度/防抖状态，新增项立即评估，存量项不动节拍。"""
        old_codes = {rule.code for rule in self._rules}
        new_codes = {rule.code for rule in remote.rules}
        for code in old_codes - new_codes:
            self._next_due.pop(code, None)
            self._in_flight.discard(code)
            self._debounce.reset(code)
        self._sources = {ds.name: ds for ds in remote.data_sources}
        self._sems = {
            name: asyncio.Semaphore(self._cfg.evaluation.per_source_concurrency)
            for name in self._sources
        }
        self._notifier.update_channels(remote.notify_channels)  # 渠道表随配置热更新
        self._rules = remote.rules
        self._config_version = remote.version
        for rule in self._rules:
            self._next_due.setdefault(rule.code, 0.0)  # 新规则下一 tick 立即评估
        logger.info(
            "配置已更新 version=%s：规则 %d 条，数据源 %d 个",
            remote.version, len(self._rules), len(self._sources),
        )

    async def _evaluate(self, rule: RuleConfig) -> None:
        self._in_flight.add(rule.code)
        try:
            # 多副本租约：抢不到说明其他实例存活并持有该规则，跳过（下轮再竞争）；
            # TTL = max(60s, 2×评估间隔)，实例死亡后租约到期自动被接管；
            # Redis 不可用 → fail-open 全量评估（宁多勿漏，平台幂等+节流兜底）。
            if self._coordinator is not None:
                lease_ttl = max(60.0, 2 * rule.eval_interval_seconds)
                if not await self._coordinator.acquire(
                        f"{rule.source}/{rule.code}", lease_ttl):
                    logger.debug("rule=%s 租约归其他实例，跳过本轮", rule.code)
                    return
            source = self._sources[rule.data_source]
            evaluator = self._evaluators[source.type]
            # 配置热替换竞态防御：数据源被删时兜底新建信号量，不让单条规则拖垮循环
            sem = self._sems.get(source.name)
            if sem is None:
                sem = asyncio.Semaphore(self._cfg.evaluation.per_source_concurrency)
            async with sem:
                result = await evaluator.evaluate(rule, source)
            await self._handle(rule, source, result)
        except Exception:  # noqa: BLE001 - 单规则故障不拖垮节拍循环
            logger.exception("规则处理异常 rule=%s", rule.code)
        finally:
            self._next_due[rule.code] = time.monotonic() + rule.eval_interval_seconds
            self._in_flight.discard(rule.code)

    async def _handle(self, rule: RuleConfig, source: DataSourceConfig,
                      result: EvaluationResult) -> None:
        window_start, window_end = current_window(rule.interval_minutes)

        if result.status == STATUS_ERROR:
            # 评估失败 = 状态未知：两路都走（§10）；平台收到 error 回报后
            # 顺延活跃 firing 的 last_seen_at（§12 二轮评审），本侧无需处理。
            # error 回报无条件发（平台统计/顺延推导窗口需要），响应携带平台节流后的 notify 决策。
            logger.warning("rule=%s 评估失败: %s", rule.code, result.error)
            data = await self._reporter.report(
                rule, REPORT_ERROR, 0, window_start, window_end,
                details=None, error=result.error,
            )
            # error 橙卡发送决策：平台指令制（多副本权威，DB 串行化保证并发回报时
            # 恰好一个副本拿到 notify=true）+ 本地限流兑底（平台不可用 data=None 时
            # 按主旁路纪律默认发，由本地 15min 限流防刷屏）。
            platform_due = (data is None) or (data.get("notify") is True)
            if platform_due and self._limiter.allow(
                    f"error:{rule.code}", ERROR_NOTIFY_MIN_INTERVAL_SECONDS):
                await self._notifier.send_error(rule, result.error)
            else:
                logger.info(
                    "rule=%s error 卡片抑制（平台节流或本地限流中），仅回报平台",
                    rule.code,
                )
            return

        if not result.hit:
            # 低于阈值静默跳过 = 恢复推导的输入（§0 决策 4），永不报 resolved
            self._debounce.record(rule.code, False, rule.for_rounds)
            return

        if not self._debounce.record(rule.code, True, rule.for_rounds):
            logger.debug("rule=%s 防抖未达标（%d/%d）",
                         rule.code, self._debounce.hits(rule.code), rule.for_rounds)
            return

        # 先回报拿平台决策，再发飞书：平台 repeat notify 协议（§4.2）——
        # 活跃 firing 30min 内重复回报返回 notify=false，由平台抑制重发（防每轮刷屏）；
        # 回报失败 / 响应缺失 / notify=true / notify 字段缺失 → 默认发（主旁路纪律：宁多勿漏）
        data = await self._reporter.report(
            rule, REPORT_FIRING, result.total_count,
            window_start, window_end, result.details,
        )

        # 通知与开单解耦（2026-09-11）：notify_enabled 是平台侧工单联动开关，
        # 执行器不消费；通知依据 = 绑定渠道 → 默认渠道。
        if data is not None and data.get("notify") is False:
            logger.info(
                "rule=%s 平台通知抑制（活跃 firing repeat 窗口内，%s），跳过飞书",
                rule.code, data.get("suppress_reason"),
            )
            return
        if not self._limiter.allow(rule.code, rule.notify_interval_minutes * 60):
            logger.info("rule=%s 本地飞书限流跳过本轮（webhook 照报）", rule.code)
            return
        await self._notifier.send_alert(rule, result, window_start, window_end)

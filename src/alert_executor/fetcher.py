"""从 bingops 平台拉取启用规则 + 数据源 + 通知渠道（§12 分发模式）。

平台契约（GET /api/v1/alerts/agent/config?version=<held>，X-Agent-Token）：

    → 200 统一信封 {"code": 0, "data": {"version": "...", "rules": [...]}}
      规则内嵌 datasource / notify_channel 对象（凭据只带引用名）；
      未绑定数据源或数据源禁用的规则平台不下发；渠道禁用时 notify_channel=null
    → 204 / data 无 rules 且 version 与持有一致 = 无变化（版本协商，§12 万级采纳项）

降级语义：拉取失败 / 被拒 / 超时 → 返回 None，调度器沿用上一份配置继续评估
（故障自愈靠下一轮重拉）；单条非法规则跳过，不拖垮整体。
平台未返回 version 时用内容 sha1 兜底，保证「无变化不重apply」。
凭据红线：远端只下发 ``password_ref``/``secret_ref`` 引用名，凭据在执行器侧 env 解。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import ValidationError

from .config import DataSourceConfig, NotifyChannelConfig, PlatformConfig, RuleConfig
from .credentials import resolve_credential

logger = logging.getLogger(__name__)

# token/契约错不重试（语义与 reporter 一致）
_NON_RETRYABLE = {401, 422}

# 平台渠道 type → 执行器渠道 type 映射（平台枚举 feishu_webhook）
_CHANNEL_TYPE_MAP = {"feishu_webhook": "feishu-bot"}


@dataclass
class RemoteConfig:
    """一次成功拉取并展平后的配置快照（执行器内部形态）。"""

    version: str
    data_sources: list[DataSourceConfig] = field(default_factory=list)
    notify_channels: list[NotifyChannelConfig] = field(default_factory=list)
    rules: list[RuleConfig] = field(default_factory=list)


def _content_hash(body: dict) -> str:
    """分发体内容指纹：平台未带 version 时的兜底（相同内容 → 视为无变化）。"""
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]  # noqa: S324 - 非安全用途


def _convert_source(item: dict, where: str) -> DataSourceConfig:
    """平台 AgentSourceConfig（含 database_name 命名差异）→ 执行器模型。"""
    item = dict(item)
    item.setdefault("database", item.pop("database_name", None))
    return DataSourceConfig.model_validate(item)


def _convert_channel(item: dict, where: str) -> NotifyChannelConfig:
    """平台 AgentNotifyChannel → 执行器模型（type/extra 字段名映射）。"""
    item = dict(item)
    item["type"] = _CHANNEL_TYPE_MAP.get(item.get("type", ""), item.get("type"))
    item.setdefault("extra_params", item.pop("extra", {}) or {})
    return NotifyChannelConfig.model_validate(item)


def parse_remote(body: dict) -> RemoteConfig:
    """解析平台分发体 data 节点：内嵌 datasource/notify_channel 展平为资源表 + 名字引用。

    按条校验，非法规则跳过（万级下单条坏 SQL 不阻塞其他规则）；
    数据源/渠道按 name 汇聚去重（平台侧引用同一行，同名必同配置）。
    """
    sources: list[DataSourceConfig] = []
    source_names: set[str] = set()
    channels: list[NotifyChannelConfig] = []
    channel_names: set[str] = set()

    rules: list[RuleConfig] = []
    seen_codes: set[str] = set()
    for i, item in enumerate(body.get("rules") or []):
        where = f"规则[{i}]"
        datasource = item.get("datasource")
        if not datasource:
            # 平台只下发已绑定启用数据源的规则；缺失视为契约异常，跳过
            logger.warning("%s %s 未内嵌数据源，跳过", where, item.get("code"))
            continue
        try:
            source = _convert_source(datasource, where)
            # 摘除内嵌对象与命名差异字段，避免与 RuleConfig 类型冲突
            rule_input = {
                key: value for key, value in item.items()
                if key not in ("datasource", "notify_channel",
                               "default_severity", "feishu_card_template")
            }
            rule_input.update({
                "data_source": source.name,
                "severity": item.get("default_severity", 2),
                "card_template": item.get("feishu_card_template"),
            })
            rule = RuleConfig.model_validate(rule_input)
        except ValidationError as e:
            logger.warning("%s 非法，跳过: %s", where, e)
            continue

        if source.name not in source_names:
            source_names.add(source.name)
            sources.append(source)

        channel_item = item.get("notify_channel")
        if channel_item is not None:
            try:
                channel = _convert_channel(channel_item, where)
            except ValidationError as e:
                logger.warning("%s 渠道非法，回退默认渠道: %s", where, e)
                channel = None
            if channel is not None:
                if channel.name not in channel_names:
                    channel_names.add(channel.name)
                    channels.append(channel)
                rule.notify_channel = channel.name

        if rule.code in seen_codes:
            logger.warning("规则 code 重复 %r，跳过后者", rule.code)
            continue
        seen_codes.add(rule.code)
        rules.append(rule)

    return RemoteConfig(
        version=str(body.get("version") or _content_hash(body)),
        data_sources=sources,
        notify_channels=channels,
        rules=rules,
    )


class AgentConfigFetcher:
    def __init__(self, client: httpx.AsyncClient, cfg: PlatformConfig) -> None:
        self._client = client
        self._cfg = cfg

    async def fetch(self, version: str | None) -> RemoteConfig | None:
        """拉取配置；返回 None 表示无变化或拉取失败（调用方沿用现有配置）。"""
        token = resolve_credential(self._cfg.token_ref)
        params = {"version": version} if version else None
        try:
            resp = await self._client.get(
                self._cfg.agent_config_url,
                params=params,
                headers={"X-Agent-Token": token},
                timeout=self._cfg.timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            logger.warning("拉取 agent/config 失败（沿用现有配置，下轮重拉）: %s", e)
            return None

        if resp.status_code in _NON_RETRYABLE:
            logger.error(
                "拉取被拒 status=%d（token/契约错，不重试）: %s",
                resp.status_code, resp.text[:200],
            )
            return None
        if resp.status_code == 204 or not resp.content:
            return None  # 版本协商：无变化
        if resp.status_code >= 400:
            logger.warning("拉取未成功 status=%d，本轮沿用现有配置", resp.status_code)
            return None

        try:
            payload: dict[str, Any] = resp.json()
        except ValueError:
            logger.error("agent/config 响应不可解析，沿用现有配置")
            return None

        # 平台统一信封 {"code": 0, "data": {...}}；兼容裸 body
        body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if not isinstance(body, dict):
            logger.error("agent/config data 节点不是对象，沿用现有配置")
            return None

        remote = parse_remote(body)
        if remote.version == version:
            return None  # 版本协商：内容无变化（含平台 version 与内容 hash 兜底）
        return remote


__all__ = ["AgentConfigFetcher", "RemoteConfig", "parse_remote"]

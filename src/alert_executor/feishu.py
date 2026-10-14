"""飞书通知（主路，发送动作在执行器）。

渠道语义（平台「通知渠道」契约）：平台只下发 ``secret_ref`` 引用名，真实 webhook
地址在执行器侧 env（URL 即 secret）；规则绑定渠道缺失/禁用时回退默认渠道（宁多勿漏）。
卡片：规则带 ``card_template``（JSON 模板）时执行器自行渲染发送，否则用内置夜莺对齐卡片
（红色 header/名称/级别/labels 平铺/触发值逐样本/时间/事件详情与查看曲线按钮，
渲染契约见设计文档 §「飞书卡片渲染契约与夜莺对齐评估 2026-10-14 定稿」）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from .config import NotifyChannelConfig, NotifyConfig
from .credentials import resolve_credential

logger = logging.getLogger(__name__)

# details 摘要进卡片的最大字符数，防止长文本刷爆卡片
MAX_DETAILS_CHARS = 800
# 显示层逐样本上限（渲染契约定稿：上限 20 条；摘要条数另由规则 detail_limit 控制）
MAX_SAMPLES_IN_CARD = 20

_SEVERITY_LABELS = {1: "严重", 2: "中等", 3: "轻微"}


def gen_sign(secret: str, timestamp: int) -> str:
    """飞书自定义机器人签名：key = f"{timestamp}\\n{secret}"，message 为空。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def build_card(title: str, template: str, md_lines: list[str],
               buttons: list[dict] | None = None) -> dict:
    """三段式卡片：header + 明细（lark_md）+ 底部按钮组（事件详情/查看曲线等）。"""
    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(md_lines)}},
        {"tag": "hr"},
        {"tag": "note", "elements": [
            {"tag": "plain_text", "content": "bingops alert-executor"},
        ]},
    ]
    if buttons:
        elements.append({"tag": "action", "actions": buttons})
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": template,
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": elements,
        },
    }


def _button(text: str, url: str, button_type: str = "default") -> dict:
    return {"tag": "button", "text": {"tag": "plain_text", "content": text},
            "type": button_type, "url": url}


def _format_details(details: Any) -> str:
    """飞书显示层格式化（2026-10-14 定稿：数据与显示分离，事件表存结构化、飞书负责人读）。

    - str（CH 单行两列文本契约）→ 原样（截断）；
    - list[dict]（PromQL vector 摘要 [{metric, value}]）→ 逐样本 lark_md 行：
      ``labels 平铺 → **value**``（夜莺「触发值」对齐：业务数值加粗，
      单序列规则效果等价夜莺触发值，多序列多一层「命中了几条序列」信息），
      上限 20 条（超出部分计数省略），输出整体 800 字符截断；
    - 其他 → str（截断）。
    """
    if details is None:
        return ""
    if isinstance(details, str):
        return _clip(details)
    if isinstance(details, list):
        lines: list[str] = []
        for item in details[:MAX_SAMPLES_IN_CARD]:
            if isinstance(item, dict):
                metric = item.get("metric") or {}
                labels = " ".join(f"{k}={v}" for k, v in metric.items())
                arrow = f"{labels} → " if labels else ""
                lines.append(f"{arrow}**{item.get('value')}**")
            else:
                lines.append(_clip(item, 200))
        text = "\n".join(lines)
        if len(details) > MAX_SAMPLES_IN_CARD:
            text += f"\n…（其余 {len(details) - MAX_SAMPLES_IN_CARD} 条序列略）"
        return _clip(text)
    return _clip(str(details))


def render_template(card_template: dict, rule: Any, result: Any,
                    window_start: datetime, window_end: datetime) -> dict:
    """对平台下发的卡片模板做 {{var}} 占位替换后返回消息体（模板结构由平台保证）。"""
    context = {
        "rule_code": rule.code,
        "rule_name": rule.name,
        "source": rule.source,
        "severity": _SEVERITY_LABELS.get(rule.severity, str(rule.severity)),
        "total_count": str(result.total_count),
        "window_start": f"{window_start:%Y-%m-%d %H:%M:%S}",
        "window_end": f"{window_end:%Y-%m-%d %H:%M:%S}",
        "details_text": _format_details(result.details),
    }

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            for name, val in context.items():
                value = value.replace("{{" + name + "}}", val)
            return value
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    return walk(card_template)


@dataclass
class _Target:
    """一次发送的解析结果。"""

    webhook_url: str
    grafana_url: str | None
    card_template: dict | None


class FeishuNotifier:
    def __init__(self, client: httpx.AsyncClient, cfg: NotifyConfig) -> None:
        self._client = client
        self._cfg = cfg
        self._channels: dict[str, NotifyChannelConfig] = {}

    def update_channels(self, channels: list[NotifyChannelConfig]) -> None:
        """随配置热更新渠道表（含禁用项，发送时回退默认）。"""
        self._channels = {channel.name: channel for channel in channels}

    def _resolve(self, rule: Any) -> _Target:
        """规则 → 发送目标：绑定渠道优先，缺失/禁用回退默认（宁多勿漏）。"""
        channel: NotifyChannelConfig | None = None
        if rule.notify_channel:
            channel = self._channels.get(rule.notify_channel)
            if channel is None or not channel.enabled:
                logger.warning(
                    "rule=%s 绑定渠道 %r 不存在或未启用，回退默认渠道",
                    rule.code, rule.notify_channel,
                )
                channel = None
        secret_ref = channel.secret_ref if channel else self._cfg.default_channel.secret_ref
        webhook_url = resolve_credential(secret_ref)
        grafana_url = rule.grafana_url or self._cfg.dashboard_url
        return _Target(webhook_url=webhook_url, grafana_url=grafana_url,
                       card_template=rule.card_template)

    def _buttons(self, event_id: int | None, grafana_url: str | None) -> list[dict]:
        """卡片按钮组：事件详情（平台前端）+ 查看曲线（Grafana/看板），按配置有无生成。"""
        buttons: list[dict] = []
        frontend = self._cfg.frontend_base_url
        if event_id and frontend:
            buttons.append(_button(
                "事件详情", f"{str(frontend).rstrip('/')}/alerts/events/{event_id}"))
        if grafana_url:
            buttons.append(_button("查看曲线", grafana_url, button_type="primary"))
        return buttons

    def _labels_line(self, rule: Any) -> str:
        """labels 平铺（static_labels 补齐 + 规则自带优先，与回报 payload 同口径）。"""
        merged = {**rule.static_labels, **rule.labels}
        return "　".join(f"{k}={v}" for k, v in merged.items())

    async def send_alert(self, rule: Any, result: Any,
                         window_start: datetime, window_end: datetime,
                         event_id: int | None = None) -> None:
        """firing 告警（CH 文本/合并形态）：优先规则卡片模板，否则内置夜莺对齐红色卡片。"""
        try:
            target = self._resolve(rule)
        except RuntimeError as e:  # 凭据 env 缺失（fail fast 语义）
            logger.error("rule=%s 飞书渠道凭据解析失败: %s", rule.code, e)
            return
        if target.card_template:
            body = render_template(target.card_template, rule, result,
                                   window_start, window_end)
        else:
            severity = _SEVERITY_LABELS.get(rule.severity, str(rule.severity))
            lines = [
                f"**规则**：{rule.name}（`{rule.code}`）",
                f"**级别**：{severity}　**命中序列**：{result.total_count}",
                f"**来源**：{rule.source}　**时间窗**：{window_start:%m-%d %H:%M} ~ {window_end:%H:%M}",
            ]
            labels_line = self._labels_line(rule)
            if labels_line:
                lines.append(f"**labels**：{labels_line}")
            lines += ["", _format_details(result.details)]
            buttons = self._buttons(event_id, target.grafana_url)
            body = build_card(f"[告警] {rule.name}", "red", lines, buttons)
        await self._post(rule, target.webhook_url, body)

    async def send_alert_series(self, rule: Any, sample: dict,
                                window_start: datetime, window_end: datetime,
                                event_id: int | None = None) -> None:
        """单序列告警卡（PromQL 多序列拆分）：序列 labels 平铺 + 触发值加粗。

        多序列拆分的价值：每条序列独立生命周期——独立触发值、独立恢复通知。
        """
        try:
            target = self._resolve(rule)
        except RuntimeError as e:
            logger.error("rule=%s 飞书渠道凭据解析失败: %s", rule.code, e)
            return
        metric = sample.get("metric") or {}
        labels_line = "　".join(f"{k}={v}" for k, v in metric.items())
        severity = _SEVERITY_LABELS.get(rule.severity, str(rule.severity))
        lines = [
            f"**规则**：{rule.name}（`{rule.code}`）",
            f"**级别**：{severity}　**触发值**：**{sample.get('value')}**",
            f"**来源**：{rule.source}　**时间窗**：{window_start:%m-%d %H:%M} ~ {window_end:%H:%M}",
        ]
        if labels_line:
            lines.append(f"**labels**：{labels_line}")
        buttons = self._buttons(event_id, target.grafana_url)
        body = build_card(f"[告警] {rule.name}", "red", lines, buttons)
        await self._post(rule, target.webhook_url, body)

    async def send_recovered(self, rule: Any, sample: dict) -> None:
        """恢复卡（绿色）：触发序列从上轮到本轮消失（夜莺「恢复通知」对齐）。"""
        try:
            target = self._resolve(rule)
        except RuntimeError as e:
            logger.error("rule=%s 飞书渠道凭据解析失败: %s", rule.code, e)
            return
        metric = sample.get("metric") or {}
        labels_line = "　".join(f"{k}={v}" for k, v in metric.items())
        lines = [
            f"**规则**：{rule.name}（`{rule.code}`）",
            "**状态**：已恢复 ✓",
        ]
        if labels_line:
            lines.append(f"**labels**：{labels_line}")
        # 恢复无对应流水事件：仅「查看曲线」按钮，无事件详情
        buttons = self._buttons(None, target.grafana_url)
        body = build_card(f"[已恢复] {rule.name}", "green", lines, buttons)
        await self._post(rule, target.webhook_url, body)

    async def send_error(self, rule: Any, error: str | None,
                         event_id: int | None = None) -> None:
        """评估失败卡片（橙色）：两路都走（§10），不发飞书的旧改动不要。"""
        try:
            target = self._resolve(rule)
        except RuntimeError as e:
            logger.error("rule=%s 飞书渠道凭据解析失败: %s", rule.code, e)
            return
        lines = [
            f"**规则**：{rule.name}（`{rule.code}`）",
            "**状态**：评估失败（状态未知，保守保持告警）",
            "",
            f"```\n{_clip(error, 500)}\n```",
        ]
        buttons = self._buttons(event_id, target.grafana_url)
        body = build_card(f"[评估失败] {rule.name}", "orange", lines, buttons)
        await self._post(rule, target.webhook_url, body)

    async def _post(self, rule: Any, webhook_url: str, body: dict) -> None:
        payload = dict(body)
        if payload.get("msg_type") == "interactive":  # 内置卡片走签名；模板体由平台保证
            secret = self._channel_sign_secret(rule)
            if secret is not None:
                timestamp = int(time.time())
                payload["timestamp"] = str(timestamp)
                payload["sign"] = gen_sign(secret, timestamp)
        try:
            resp = await self._client.post(webhook_url, json=payload)
            resp.raise_for_status()
            result = resp.json()
            if result.get("code") not in (None, 0):
                logger.error("飞书返回错误: %s", result)
        except Exception as e:  # noqa: BLE001 - 通知失败打日志即可，不阻断评估循环
            logger.error("飞书发送失败: %s", e)

    def _channel_sign_secret(self, rule: Any) -> str | None:
        """签名密钥：一期约定 webhook 地址即凭据、无独立签名密钥，返回 None。
        渠道 extra_params.sign_key_ref 可选扩展（env 引用名）。"""
        channel = self._channels.get(rule.notify_channel) if rule.notify_channel else None
        sign_ref = (channel.extra_params or {}).get("sign_key_ref") if channel else None
        if not sign_ref:
            return None
        return resolve_credential(str(sign_ref))


def _clip(value: Any, limit: int = MAX_DETAILS_CHARS) -> str:
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= limit else text[:limit] + "…(截断)"


__all__ = ["FeishuNotifier", "build_card", "gen_sign", "render_template",
           "_format_details"]

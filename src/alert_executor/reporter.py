"""bingops 平台 webhook 回报（设计文档 §4 契约 / §10 骨架）。

纪律：主旁路（§0 决策 6）——回报失败只打日志，绝不影响飞书主路；
at-least-once 由「下轮评估仍达标自然重报」实现，不做本地排队重试。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import httpx

from .config import PlatformConfig
from .credentials import resolve_credential
from .models import REPORT_ERROR, REPORT_FIRING

logger = logging.getLogger(__name__)

# 契约/配置错：打 error 不重试（重试无意义）
_NON_RETRYABLE = {401, 422}


def build_payload(rule: Any, status: str, window_start: datetime,
                  window_end: datetime, total_count: int,
                  details: Any, error: str | None = None) -> dict:
    """§4.1 请求体契约。``window_*`` ISO8601 带时区，平台统一转 UTC 存。
    labels = static_labels（平台侧补齐）∪ labels（后者优先）。"""
    return {
        "source": rule.source,
        "rule_code": rule.code,
        "rule_name": rule.name,
        "status": status,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "total_count": total_count,
        "severity": rule.severity,
        "labels": {**rule.static_labels, **rule.labels},
        "details": details,
        "error": error,
    }


def current_window(interval_minutes: int) -> tuple[datetime, datetime]:
    """评估窗口：now - interval_minutes ~ now，带本地时区偏移。"""
    end = datetime.now().astimezone()
    return end - timedelta(minutes=interval_minutes), end


class PlatformReporter:
    def __init__(self, client: httpx.AsyncClient, cfg: PlatformConfig) -> None:
        self._client = client
        self._cfg = cfg

    async def report(self, rule: Any, status: str, total_count: int,
                     window_start: datetime, window_end: datetime,
                     details: Any, error: str | None = None) -> dict | None:
        """回报事件；返回平台响应 data（§4.2），失败/不可解析返回 None。"""
        token = resolve_credential(self._cfg.token_ref)
        payload = build_payload(rule, status, window_start, window_end,
                                total_count, details, error)
        try:
            resp = await self._client.post(
                self._cfg.webhook_url,
                json=payload,
                headers={"X-Agent-Token": token},
                timeout=self._cfg.timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            logger.warning("回报 bingops 失败（不影响飞书主路，下轮达标自然重报）: %s", e)
            return None

        if resp.status_code in _NON_RETRYABLE:
            logger.error(
                "回报被拒 status=%d（契约/配置错，不重试）rule=%s: %s",
                resp.status_code, rule.code, resp.text[:200],
            )
            return None
        if resp.status_code >= 400:
            logger.warning(
                "回报未成功 status=%d rule=%s，本轮放弃: %s",
                resp.status_code, rule.code, resp.text[:200],
            )
            return None
        try:
            return resp.json().get("data")
        except ValueError:
            logger.warning("回报响应不可解析（视为已受理）rule=%s", rule.code)
            return None


__all__ = ["PlatformReporter", "build_payload", "current_window",
           "REPORT_FIRING", "REPORT_ERROR"]

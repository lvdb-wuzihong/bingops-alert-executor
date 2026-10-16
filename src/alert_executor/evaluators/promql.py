"""victoria / prometheus PromQL 评估器。

表达式自带比较，vector 非空即触发（夜莺退役后指标评估由本评估器承担，
设计文档 §0 决策 2）；threshold / interval_minutes 不参与——窗口写进 PromQL ``[5m]``。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from ..credentials import resolve_credential
from ..models import STATUS_OK, EvaluationResult

logger = logging.getLogger(__name__)

# 连接级瞬时故障（跨境链路抖动 / 陈旧 keepalive 被对端关闭）：GET 查询幂等，
# 重试一次自愈；读超时不在此列（说明查询确实慢，重试只会翻倍墙钟预算）。
_RETRYABLE_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)


class PromQLEvaluator:
    def __init__(self, client: httpx.AsyncClient, timeout_seconds: float) -> None:
        self._client = client
        self._timeout = timeout_seconds

    async def evaluate(self, rule: Any, source: Any) -> EvaluationResult:
        try:
            return await asyncio.wait_for(self._evaluate(rule, source),
                                          timeout=self._timeout)
        except Exception as e:  # noqa: BLE001 - 评估失败统一转 error 回报（两路都走）
            logger.warning("promql 评估失败 rule=%s: %s", rule.code, e)
            return EvaluationResult.from_error(e)

    async def _evaluate(self, rule: Any, source: Any) -> EvaluationResult:
        started = time.perf_counter()
        try:
            resp = await self._request(rule, source)
        except _RETRYABLE_ERRORS as e:
            logger.warning("promql 连接级失败，重试一次 rule=%s: %s", rule.code, e)
            resp = await self._request(rule, source)
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "success":
            raise ValueError(f"查询接口返回异常 status={body.get('status')!r}")
        vector = body.get("data", {}).get("result") or []
        total = len(vector)
        # 慢查询观测：耗时 ≥ 超时预算一半打 warning（跨境链路诊断可见）
        elapsed = time.perf_counter() - started
        if elapsed >= self._timeout / 2:
            logger.warning("promql 慢查询 rule=%s 耗时=%.1fs（超时阈值=%.0fs）",
                           rule.code, elapsed, self._timeout)
        return EvaluationResult(
            status=STATUS_OK,
            hit=total > 0,
            total_count=total,
            details=_summarize(vector, limit=rule.detail_limit),
        )

    async def _request(self, rule: Any, source: Any) -> httpx.Response:
        return await self._client.get(
            f"{source.normalized_url()}/api/v1/query",
            params={"query": rule.eval_sql},
            auth=_basic_auth(source),
            timeout=self._timeout,
        )


def _basic_auth(source: Any) -> tuple[str, str] | None:
    """数据源凭据 → httpx basic auth（vmauth/反代鉴权场景）。

    NO_AUTH（无密码）→ None（匿名，不带 Authorization 头）；
    密码经 password_ref env 解引用（凭据红线），用户名缺省 default。
    """
    password = resolve_credential(source.password_ref)
    if password is None:
        return None
    return (source.username or "default", password)


def _summarize(vector: list[dict], limit: int) -> list[dict]:
    """把 vector 压缩为可读摘要：metric labels + value；条数取规则 detail_limit（平台分发）。"""
    samples = []
    for item in vector[:limit]:
        value_pair = item.get("value") or [None, None]
        samples.append(
            {"metric": item.get("metric", {}), "value": value_pair[1]}
        )
    return samples

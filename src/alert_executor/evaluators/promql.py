"""victoria / prometheus PromQL 评估器。

表达式自带比较，vector 非空即触发（夜莺退役后指标评估由本评估器承担，
设计文档 §0 决策 2）；threshold / interval_minutes 不参与——窗口写进 PromQL ``[5m]``。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ..credentials import resolve_credential
from ..models import STATUS_OK, EvaluationResult

logger = logging.getLogger(__name__)


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
        resp = await self._client.get(
            f"{source.normalized_url()}/api/v1/query",
            params={"query": rule.eval_sql},
            auth=_basic_auth(source),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "success":
            raise ValueError(f"查询接口返回异常 status={body.get('status')!r}")
        vector = body.get("data", {}).get("result") or []
        total = len(vector)
        return EvaluationResult(
            status=STATUS_OK,
            hit=total > 0,
            total_count=total,
            details=_summarize(vector, limit=rule.detail_limit),
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

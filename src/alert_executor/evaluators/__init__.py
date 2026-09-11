"""按数据源 type 分发评估器（双评估器，设计文档 §0 决策 2）。"""

from __future__ import annotations

from typing import Any

import httpx

from ..config import EvaluationConfig
from .clickhouse import ClickHouseEvaluator
from .promql import PromQLEvaluator

EVALUATOR_BY_TYPE = {
    "clickhouse": ClickHouseEvaluator,
    "victoria": PromQLEvaluator,
    "prometheus": PromQLEvaluator,
}


def build_evaluators(client: httpx.AsyncClient,
                     evaluation_cfg: EvaluationConfig) -> dict[str, Any]:
    """返回 {source_type: evaluator}；timeout 为单条评估硬超时（万级演进预留 §12）。"""
    return {
        "clickhouse": ClickHouseEvaluator(evaluation_cfg.timeout_seconds),
        "victoria": PromQLEvaluator(client, evaluation_cfg.timeout_seconds),
        "prometheus": PromQLEvaluator(client, evaluation_cfg.timeout_seconds),
    }


__all__ = ["build_evaluators", "ClickHouseEvaluator", "PromQLEvaluator"]

"""clickhouse SQL 评估器。

eval_sql 契约：单行两列 ``error_count + log_details``（参考 ck-log-alert 存量 SQL 原样可贴，
设计文档 §12）；``{window_minutes}`` 占位替换为规则查询窗口。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from ..credentials import resolve_credential
from ..models import STATUS_OK, EvaluationResult

logger = logging.getLogger(__name__)

WINDOW_PLACEHOLDER = "{window_minutes}"

# 生产查询实现：clickhouse-connect 同步客户端放到线程执行。
# password=None（NO_AUTH）时不带认证参数；timeout 秒级硬超时（连接+读写），
# 评估器外层另有 asyncio.wait_for 二次兜底。
QueryFn = Callable[[str, str, str | None, float], list[tuple]]


def _default_query_fn(host: str, port: int, secure: bool, database: str | None,
                      username: str | None, sql: str, password: str | None,
                      timeout_seconds: float) -> list[tuple]:
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=host,
        port=port,
        secure=secure,
        database=database,
        username=username,
        password=password,
        connect_timeout=int(timeout_seconds),
        send_receive_timeout=int(timeout_seconds),
    )
    try:
        return client.query(sql).result_rows
    finally:
        client.close()


class ClickHouseEvaluator:
    """阈值判定：error_count >= threshold 触发；低于阈值静默跳过（恢复推导归平台）。"""

    def __init__(self, timeout_seconds: float,
                 query_fn: QueryFn | None = None) -> None:
        self._timeout = timeout_seconds
        self._query_fn = query_fn

    async def evaluate(self, rule: Any, source: Any) -> EvaluationResult:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._evaluate_sync, rule, source),
                timeout=self._timeout,
            )
        except Exception as e:  # noqa: BLE001 - 评估失败统一转 error 回报（两路都走）
            logger.warning("clickhouse 评估失败 rule=%s: %s", rule.code, e)
            return EvaluationResult.from_error(e)

    def _evaluate_sync(self, rule: Any, source: Any) -> EvaluationResult:
        password = resolve_credential(source.password_ref)
        sql = rule.eval_sql.replace(WINDOW_PLACEHOLDER, str(rule.interval_minutes))
        query_fn = self._query_fn or _default_query_fn
        rows = query_fn(source.host, source.port, source.secure, source.database,
                        source.username, sql, password, self._timeout)
        if len(rows) != 1 or len(rows[0]) != 2:
            raise ValueError(
                f"SQL 契约违反：期望单行两列 error_count+log_details，"
                f"得到 {len(rows)} 行 x "
                f"{len(rows[0]) if rows else 0} 列"
            )
        error_count, log_details = rows[0]
        total = int(error_count)
        return EvaluationResult(
            status=STATUS_OK,
            hit=total >= rule.threshold,
            total_count=total,
            details=log_details,
        )

"""双评估器契约：clickhouse 单行两列 / promql vector 非空触发。"""

from __future__ import annotations

import httpx
import pytest
import respx

from alert_executor.evaluators.clickhouse import ClickHouseEvaluator
from alert_executor.evaluators.promql import PromQLEvaluator
from conftest import make_rule, make_source


class TestClickHouseEvaluator:
    async def test_window_placeholder_replaced(self):
        captured = {}
        sql_with_window = (
            "SELECT count() FROM t WHERE ts > now() - INTERVAL {window_minutes} MINUTE"
        )

        def query_fn(host, port, secure, database, username, sql, password, timeout):
            captured["sql"] = sql
            captured["password"] = password
            return [(3, "detail-line")]

        rule = make_rule(interval_minutes=7, threshold=1, eval_sql=sql_with_window)
        evaluator = ClickHouseEvaluator(10, query_fn=query_fn)
        result = await evaluator.evaluate(rule, make_source())
        assert "{window_minutes}" not in captured["sql"]
        assert "INTERVAL 7 MINUTE" in captured["sql"] or " 7 " in captured["sql"]
        assert result.status == "ok"
        assert result.hit is True
        assert result.total_count == 3
        assert result.details == "detail-line"
        assert captured["password"] is None  # NO_AUTH 不带凭据

    async def test_below_threshold_is_miss(self):
        evaluator = ClickHouseEvaluator(10, query_fn=lambda *a: [(0, None)])
        result = await evaluator.evaluate(make_rule(threshold=1), make_source())
        assert result.status == "ok"
        assert result.hit is False

    @pytest.mark.parametrize("rows", [[], [(1, "a"), (1, "b")], [(1, "a", "extra")]])
    async def test_contract_violation_becomes_error(self, rows):
        evaluator = ClickHouseEvaluator(10, query_fn=lambda *a: rows)
        result = await evaluator.evaluate(make_rule(), make_source())
        assert result.status == "error"
        assert "契约违反" in result.error

    async def test_missing_credential_becomes_error_not_raise(self, monkeypatch):
        monkeypatch.delenv("MISSING_PW", raising=False)
        evaluator = ClickHouseEvaluator(10, query_fn=lambda *a: [])
        source = make_source(password_ref="MISSING_PW")
        result = await evaluator.evaluate(make_rule(), source)
        assert result.status == "error"
        assert "MISSING_PW" in result.error

    async def test_query_exception_becomes_error(self):
        def boom(*a):
            raise ConnectionError("ch down")

        evaluator = ClickHouseEvaluator(10, query_fn=boom)
        result = await evaluator.evaluate(make_rule(), make_source())
        assert result.status == "error"
        assert "ConnectionError" in result.error


class TestPromQLEvaluator:
    @respx.mock
    async def test_nonempty_vector_hits(self):
        respx.get("http://vm.internal:8428/api/v1/query").respond(
            json={"status": "success", "data": {"result": [
                {"metric": {"instance": "n1"}, "value": [1750000000, "0.95"]},
            ]}}
        )
        async with httpx.AsyncClient() as client:
            evaluator = PromQLEvaluator(client, 10)
            result = await evaluator.evaluate(make_rule(), make_source(type_="victoria"))
        assert result.status == "ok"
        assert result.hit is True
        assert result.total_count == 1
        assert result.details == [{"metric": {"instance": "n1"}, "value": "0.95"}]

    @respx.mock
    async def test_empty_vector_is_miss(self):
        respx.get("http://vm.internal:8428/api/v1/query").respond(
            json={"status": "success", "data": {"result": []}}
        )
        async with httpx.AsyncClient() as client:
            result = await PromQLEvaluator(client, 10).evaluate(
                make_rule(), make_source(type_="victoria"))
        assert result.status == "ok"
        assert result.hit is False

    @respx.mock
    async def test_query_error_becomes_error(self):
        respx.get("http://vm.internal:8428/api/v1/query").respond(status_code=502)
        async with httpx.AsyncClient() as client:
            result = await PromQLEvaluator(client, 10).evaluate(
                make_rule(), make_source(type_="victoria"))
        assert result.status == "error"

    @respx.mock
    async def test_truncated_details_by_rule_detail_limit(self):
        """摘要条数取规则 detail_limit（平台分发字段）。"""
        vector = [{"metric": {"i": str(i)}, "value": [0, "1"]} for i in range(30)]
        respx.get("http://vm.internal:8428/api/v1/query").respond(
            json={"status": "success", "data": {"result": vector}}
        )
        async with httpx.AsyncClient() as client:
            result = await PromQLEvaluator(client, 10).evaluate(
                make_rule(detail_limit=5), make_source(type_="victoria"))
        assert len(result.details) == 5

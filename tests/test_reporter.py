"""webhook 回报契约（§4）：payload 字段、错误语义、主旁路纪律。"""

from __future__ import annotations

import logging

import httpx
import pytest
import respx

from alert_executor.config import PlatformConfig
from alert_executor.models import REPORT_ERROR, REPORT_FIRING
from alert_executor.reporter import PlatformReporter, build_payload, current_window
from conftest import make_rule

WEBHOOK = "http://bingops.internal:8000/api/v1/alerts/webhook"


@pytest.fixture(autouse=True)
def _token_env(monkeypatch):
    """token_ref 是 env 引用（红线 4），本文件所有测试统一预设。"""
    monkeypatch.setenv("T_OK", "s3cret-token")


def make_reporter(client: httpx.AsyncClient) -> PlatformReporter:
    return PlatformReporter(client, PlatformConfig(base_url="http://bingops.internal:8000",
                                                   token_ref="T_OK"))


class TestBuildPayload:
    def test_contract_fields(self):
        rule = make_rule(source="ck-log-alert", severity=1, labels={"env": "prod"})
        start, end = current_window(rule.interval_minutes)
        payload = build_payload(rule, REPORT_FIRING, start, end, 42,
                                "detail", error=None)
        assert payload["source"] == "ck-log-alert"
        assert payload["rule_code"] == "r1"
        assert payload["rule_name"] == "规则 r1"
        assert payload["status"] == "firing"
        assert payload["total_count"] == 42
        assert payload["severity"] == 1
        assert payload["labels"] == {"env": "prod"}
        assert payload["details"] == "detail"
        assert payload["error"] is None
        # ISO8601 带时区偏移
        assert start.isoformat().endswith(("+08:00", "Z")) or "+" in start.isoformat()


class TestReportErrorSemantics:
    @respx.mock
    async def test_success_returns_data(self):
        route = respx.post(WEBHOOK).respond(
            json={"code": 0, "message": "ok",
                  "data": {"event_id": 123, "notify": True}})
        async with httpx.AsyncClient() as client:
            reporter = make_reporter(client)
            rule = make_rule()
            data = await reporter.report(rule, REPORT_FIRING, 1, *current_window(1),
                                         details="d")
        assert data == {"event_id": 123, "notify": True}
        assert route.called
        request = route.calls[0].request
        assert request.headers["X-Agent-Token"] == "s3cret-token"
        assert b'"rule_code"' in request.content

    @respx.mock
    async def test_401_not_retried_logged_as_error(self, caplog):
        respx.post(WEBHOOK).respond(status_code=401, text="bad token")
        async with httpx.AsyncClient() as client:
            with caplog.at_level(logging.ERROR, logger="alert_executor.reporter"):
                data = await make_reporter(client).report(
                    make_rule(), REPORT_FIRING, 1, *current_window(1), details="d")
        assert data is None
        assert any("不重试" in r.message for r in caplog.records)

    @respx.mock
    async def test_422_not_retried(self):
        respx.post(WEBHOOK).respond(status_code=422, text="schema")
        async with httpx.AsyncClient() as client:
            data = await make_reporter(client).report(
                make_rule(), REPORT_FIRING, 1, *current_window(1), details="d")
        assert data is None

    @respx.mock
    async def test_5xx_gives_up_this_round(self, caplog):
        respx.post(WEBHOOK).respond(status_code=503, text="down")
        async with httpx.AsyncClient() as client:
            with caplog.at_level(logging.WARNING, logger="alert_executor.reporter"):
                data = await make_reporter(client).report(
                    make_rule(), REPORT_FIRING, 1, *current_window(1), details="d")
        assert data is None
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    @respx.mock
    async def test_connect_error_never_raises(self):
        respx.post(WEBHOOK).mock(side_effect=httpx.ConnectError("refused"))
        async with httpx.AsyncClient() as client:
            data = await make_reporter(client).report(
                make_rule(), REPORT_FIRING, 1, *current_window(1), details="d")
        assert data is None  # 主旁路：不影响飞书主路

    @respx.mock
    async def test_unparsable_response_treated_as_accepted(self, caplog):
        respx.post(WEBHOOK).respond(status_code=200, text="<html>not json</html>")
        async with httpx.AsyncClient() as client:
            data = await make_reporter(client).report(
                make_rule(), REPORT_FIRING, 1, *current_window(1), details="d")
        assert data is None

    async def test_error_status_payload_carries_error(self):
        rule = make_rule()
        start, end = current_window(1)
        payload = build_payload(rule, REPORT_ERROR, start, end, 0,
                                details=None, error="boom")
        assert payload["status"] == "error"
        assert payload["error"] == "boom"

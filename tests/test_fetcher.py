"""agent/config 拉取：统一信封解包、平台内嵌结构展平、版本协商（含 hash 兜底）。"""

from __future__ import annotations

import httpx
import pytest
import respx

from alert_executor.config import PlatformConfig
from alert_executor.fetcher import AgentConfigFetcher, parse_remote
from conftest import make_source

BASE = "http://bingops.internal:8000"
AGENT_URL = f"{BASE}/api/v1/alerts/agent/config"

# 平台 AgentConfigResponse 的真实形状（内嵌 datasource/notify_channel）
PLATFORM_RULE = {
    "id": 1,
    "source": "bingops",
    "code": "pdm_error_log_alert",
    "name": "pdm错误日志告警",
    "interval_minutes": 1,
    "threshold": 10,
    "for_rounds": 2,
    "detail_limit": 5,
    "eval_interval_seconds": 60,
    "eval_sql": "SELECT count(), any(message) FROM logs WHERE ts > now() - INTERVAL 1 MINUTE",
    "stale_minutes": 5,
    "default_severity": 1,
    "group_id": None,
    "static_labels": {"env": "prod"},
    "grafana_url": "https://grafana.example.com/explore",
    "feishu_card_template": {"msg_type": "text", "content": {"text": "x"}},
    "notify_enabled": True,
    "datasource": {
        "name": "test-aliyun-clickhouse",
        "type": "clickhouse",
        "host": "cc-xxx.aliyuncs.com",
        "port": 8443,
        "database_name": "logs",
        "username": "alert_ro",
        "password_ref": "PROD_CH_READONLY",
        "secure": True,
    },
    "notify_channel": {
        "name": "test-feishu-robot",
        "type": "feishu_webhook",
        "secret_ref": "TEST_FEISHU_ROBOT",
        "extra": {"ats": ["13800000000"]},
    },
}


def platform_body(version: str | None = None, rules: list[dict] | None = None) -> dict:
    body = {"rules": [PLATFORM_RULE] if rules is None else rules}
    if version is not None:
        body["version"] = version
    return body


def enveloped(body: dict) -> dict:
    return {"code": 0, "message": "ok", "data": body}


def make_fetcher(client: httpx.AsyncClient) -> AgentConfigFetcher:
    return AgentConfigFetcher(client, PlatformConfig(base_url=BASE, token_ref="T_OK"))


class TestParseRemote:
    def test_flatten_and_field_mapping(self):
        remote = parse_remote(platform_body())
        # 内嵌 datasource → 展平资源表 + 名字引用；database_name → database
        assert [ds.name for ds in remote.data_sources] == ["test-aliyun-clickhouse"]
        ds = remote.data_sources[0]
        assert ds.database == "logs" and ds.secure is True
        assert ds.password_ref == "PROD_CH_READONLY"
        rule = remote.rules[0]
        assert rule.data_source == "test-aliyun-clickhouse"
        # default_severity → severity；feishu_card_template → card_template
        assert rule.severity == 1 and rule.threshold == 10 and rule.for_rounds == 2
        assert rule.detail_limit == 5
        assert rule.card_template == {"msg_type": "text", "content": {"text": "x"}}
        # 内嵌渠道 → 汇聚 + 名字引用；feishu_webhook → feishu-bot；extra → extra_params
        assert [ch.name for ch in remote.notify_channels] == ["test-feishu-robot"]
        assert remote.notify_channels[0].type == "feishu-bot"
        assert remote.notify_channels[0].extra_params == {"ats": ["13800000000"]}
        assert rule.notify_channel == "test-feishu-robot"

    def test_shared_source_deduplicated(self):
        second = dict(PLATFORM_RULE, code="r2", datasource=PLATFORM_RULE["datasource"])
        remote = parse_remote(platform_body(rules=[PLATFORM_RULE, second]))
        assert len(remote.data_sources) == 1  # 同源汇聚
        assert [r.code for r in remote.rules] == ["pdm_error_log_alert", "r2"]

    def test_null_channel_keeps_rule_with_default_notify(self):
        rule_item = dict(PLATFORM_RULE, notify_channel=None)
        remote = parse_remote(platform_body(rules=[rule_item]))
        assert remote.rules[0].notify_channel is None  # 通知由执行器默认处理
        assert remote.notify_channels == []

    def test_missing_datasource_skipped(self):
        rule_item = dict(PLATFORM_RULE, datasource=None)
        remote = parse_remote(platform_body(rules=[rule_item]))
        assert remote.rules == []

    def test_invalid_rule_skipped(self):
        bad = dict(PLATFORM_RULE, threshold="not-an-int")
        remote = parse_remote(platform_body(rules=[bad, PLATFORM_RULE]))
        assert [r.code for r in remote.rules] == ["pdm_error_log_alert"]

    def test_invalid_channel_falls_back_to_default(self):
        rule_item = dict(PLATFORM_RULE,
                         notify_channel={"name": "bad"})  # 缺 secret_ref
        remote = parse_remote(platform_body(rules=[rule_item]))
        assert remote.rules[0].notify_channel is None
        assert remote.notify_channels == []

    def test_version_hash_fallback_stable(self):
        """平台未带 version 时用内容指纹兜底：相同内容两次解析版本一致。"""
        v1 = parse_remote(platform_body()).version
        v2 = parse_remote(platform_body()).version
        assert v1 == v2 and len(v1) == 12
        v_changed = parse_remote(platform_body(rules=[dict(PLATFORM_RULE, threshold=99)])).version
        assert v_changed != v1


class TestFetch:
    @respx.mock
    async def test_envelope_unwrapped_and_version_sent(self, monkeypatch):
        monkeypatch.setenv("T_OK", "s3cret")
        route = respx.get(AGENT_URL).respond(
            json=enveloped(platform_body(version="v8")))
        async with httpx.AsyncClient() as client:
            remote = await make_fetcher(client).fetch("v7")
        assert route.calls[0].request.url.params["version"] == "v7"
        assert route.calls[0].request.headers["X-Agent-Token"] == "s3cret"
        assert remote is not None and remote.version == "v8"
        assert len(remote.rules) == 1

    @respx.mock
    async def test_same_version_means_no_change(self, monkeypatch):
        monkeypatch.setenv("T_OK", "s3cret")
        respx.get(AGENT_URL).respond(json=enveloped(platform_body(version="v7")))
        async with httpx.AsyncClient() as client:
            assert await make_fetcher(client).fetch("v7") is None

    @respx.mock
    async def test_hash_fallback_dedupes_when_platform_has_no_version(self, monkeypatch):
        monkeypatch.setenv("T_OK", "s3cret")
        body = platform_body()  # 无 version 字段
        respx.get(AGENT_URL).respond(json=enveloped(body))
        async with httpx.AsyncClient() as client:
            first = await make_fetcher(client).fetch(None)
            assert first is not None and len(first.rules) == 1
            # 相同内容 → hash 一致 → 不再 apply
            assert await make_fetcher(client).fetch(first.version) is None

    @respx.mock
    async def test_204_empty_means_no_change(self, monkeypatch):
        monkeypatch.setenv("T_OK", "s3cret")
        respx.get(AGENT_URL).respond(status_code=204)
        async with httpx.AsyncClient() as client:
            assert await make_fetcher(client).fetch("v7") is None

    @respx.mock
    async def test_401_no_retry_returns_none(self, monkeypatch):
        monkeypatch.setenv("T_OK", "s3cret")
        respx.get(AGENT_URL).respond(status_code=401, text="bad token")
        async with httpx.AsyncClient() as client:
            assert await make_fetcher(client).fetch(None) is None

    @respx.mock
    async def test_5xx_returns_none(self, monkeypatch):
        monkeypatch.setenv("T_OK", "s3cret")
        respx.get(AGENT_URL).respond(status_code=503)
        async with httpx.AsyncClient() as client:
            assert await make_fetcher(client).fetch(None) is None

    @respx.mock
    async def test_connect_error_returns_none(self, monkeypatch):
        monkeypatch.setenv("T_OK", "s3cret")
        respx.get(AGENT_URL).mock(side_effect=httpx.ConnectError("refused"))
        async with httpx.AsyncClient() as client:
            assert await make_fetcher(client).fetch(None) is None

    @respx.mock
    async def test_unparsable_body_returns_none(self, monkeypatch):
        monkeypatch.setenv("T_OK", "s3cret")
        respx.get(AGENT_URL).respond(status_code=200, text="<html>")
        async with httpx.AsyncClient() as client:
            assert await make_fetcher(client).fetch(None) is None

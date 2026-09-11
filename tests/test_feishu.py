"""飞书通知：渠道解析（绑定→默认回退）、签名、三段式卡片、模板渲染。"""

from __future__ import annotations

import httpx
import pytest
import respx

from alert_executor.config import FeishuBotChannel, NotifyChannelConfig, NotifyConfig
from alert_executor.feishu import (FeishuNotifier, build_card, gen_sign,
                                   render_template)
from alert_executor.models import EvaluationResult, STATUS_OK
from conftest import make_rule

HOOK_DEFAULT = "https://open.feishu.cn/hook/default"
HOOK_BOUND = "https://open.feishu.cn/hook/bound"


def make_notifier(client: httpx.AsyncClient,
                  default_secret_ref: str = "DEFAULT_HOOK") -> FeishuNotifier:
    return FeishuNotifier(client, NotifyConfig(
        default_channel=FeishuBotChannel(secret_ref=default_secret_ref),
        dashboard_url="https://grafana.example.com",
    ))


def hit_result() -> EvaluationResult:
    return EvaluationResult(status=STATUS_OK, hit=True, total_count=3, details="d")


def _fake_dt():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def test_gen_sign_deterministic():
    assert gen_sign("secret", 1700000000) == gen_sign("secret", 1700000000)
    assert gen_sign("secret", 1700000000) != gen_sign("secret", 1700000001)


def test_build_card_three_sections():
    card = build_card("[告警] t", "red", ["line1", "line2"], "https://g.internal")
    body = card["card"]
    assert body["header"]["template"] == "red"
    assert "line1" in body["elements"][0]["text"]["content"]
    assert body["elements"][1]["tag"] == "hr"
    assert body["elements"][-1]["actions"][0]["url"] == "https://g.internal"


def test_build_card_without_dashboard_has_no_action():
    card = build_card("[告警] t", "red", ["line"])
    assert not any(el["tag"] == "action" for el in card["card"]["elements"])


class TestChannelResolution:
    @respx.mock
    async def test_bound_channel_used(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_HOOK", HOOK_DEFAULT)
        monkeypatch.setenv("BOUND_HOOK", HOOK_BOUND)
        route_default = respx.post(HOOK_DEFAULT).respond(json={"code": 0})
        route_bound = respx.post(HOOK_BOUND).respond(json={"code": 0})
        rule = make_rule(notify_channel="ops")
        async with httpx.AsyncClient() as client:
            notifier = make_notifier(client)
            notifier.update_channels([NotifyChannelConfig(
                name="ops", secret_ref="BOUND_HOOK")])
            await notifier.send_alert(rule, hit_result(), _fake_dt(), _fake_dt())
        assert route_bound.called and not route_default.called

    @respx.mock
    async def test_unbound_rule_uses_default_channel(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_HOOK", HOOK_DEFAULT)
        route = respx.post(HOOK_DEFAULT).respond(json={"code": 0})
        async with httpx.AsyncClient() as client:
            notifier = make_notifier(client)
            notifier.update_channels([NotifyChannelConfig(name="ops",
                                                          secret_ref="BOUND_HOOK")])
            await notifier.send_alert(make_rule(), hit_result(), _fake_dt(), _fake_dt())
        assert route.called  # 未绑定渠道 → 默认渠道被调用

    @respx.mock
    async def test_missing_or_disabled_channel_falls_back(self, monkeypatch):
        """绑定渠道不存在/未启用 → 回退默认渠道（宁多勿漏）。"""
        monkeypatch.setenv("DEFAULT_HOOK", HOOK_DEFAULT)
        route_default = respx.post(HOOK_DEFAULT).respond(json={"code": 0})
        route_bound = respx.post(HOOK_BOUND).respond(json={"code": 0})
        async with httpx.AsyncClient() as client:
            notifier = make_notifier(client)
            notifier.update_channels([
                NotifyChannelConfig(name="ghost", secret_ref="BOUND_HOOK",
                                    enabled=False),
            ])
            await notifier.send_alert(make_rule(notify_channel="ghost"),
                                      hit_result(), _fake_dt(), _fake_dt())
            await notifier.send_alert(make_rule(notify_channel="nope"),
                                      hit_result(), _fake_dt(), _fake_dt())
        assert route_default.called and not route_bound.called

    @respx.mock
    async def test_missing_env_logs_error_never_raises(self, monkeypatch):
        monkeypatch.delenv("DEFAULT_HOOK", raising=False)
        route = respx.post(HOOK_DEFAULT).respond(json={"code": 0})
        async with httpx.AsyncClient() as client:
            notifier = make_notifier(client)
            await notifier.send_alert(make_rule(), hit_result(), _fake_dt(), _fake_dt())
        assert not route.called  # env 缺失 fail fast 被捕获为 error 日志，不抛

    @respx.mock
    async def test_grafana_url_priority(self, monkeypatch):
        """rule.grafana_url 优先于 bootstrap dashboard_url。"""
        monkeypatch.setenv("DEFAULT_HOOK", HOOK_DEFAULT)
        route = respx.post(HOOK_DEFAULT).respond(json={"code": 0})
        rule = make_rule(grafana_url="https://grafana.rule/pdmc")
        async with httpx.AsyncClient() as client:
            await make_notifier(client).send_alert(rule, hit_result(),
                                                   _fake_dt(), _fake_dt())
        assert "https://grafana.rule/pdmc" in route.calls[0].request.content.decode()

    @respx.mock
    async def test_send_failure_never_raises(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_HOOK", HOOK_DEFAULT)
        respx.post(HOOK_DEFAULT).mock(side_effect=httpx.ConnectError("refused"))
        async with httpx.AsyncClient() as client:
            await make_notifier(client).send_alert(make_rule(), hit_result(),
                                                   _fake_dt(), _fake_dt())


class TestCardTemplate:
    def test_render_replaces_vars(self):
        template = {
            "msg_type": "interactive",
            "card": {
                "header": {"template": "red",
                           "title": {"tag": "plain_text",
                                     "content": "{{rule_name}}"}},
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md",
                                            "content": "{{rule_code}}: {{total_count}}"}},
                    {"tag": "div", "text": {"tag": "lark_md",
                                            "content": "{{details_text}}"}},
                ],
            },
        }
        rule = make_rule(code="pdm", threshold=9)
        body = render_template(template, rule, hit_result(), _fake_dt(), _fake_dt())
        title = body["card"]["header"]["title"]["content"]
        text = body["card"]["elements"][0]["text"]["content"]
        assert title == "规则 pdm"
        assert text == "pdm: 3"
        assert "{{" not in str(body)

    @respx.mock
    async def test_template_body_posted_as_is(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_HOOK", HOOK_DEFAULT)
        route = respx.post(HOOK_DEFAULT).respond(json={"code": 0})
        template = {"msg_type": "text",
                    "content": {"text": "{{rule_name}} {{total_count}}"}}
        rule = make_rule(notify_channel="ops", card_template=template)
        async with httpx.AsyncClient() as client:
            notifier = make_notifier(client)
            notifier.update_channels([NotifyChannelConfig(
                name="ops", secret_ref="DEFAULT_HOOK")])
            await notifier.send_alert(rule, hit_result(), _fake_dt(), _fake_dt())
        sent = route.calls[0].request.content.decode()
        assert '"msg_type": "text"' in sent or '"msg_type":"text"' in sent
        assert "sign" not in sent  # 模板体不走内置签名流程（结构由平台保证）

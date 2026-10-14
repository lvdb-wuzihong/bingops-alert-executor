"""飞书通知：渠道解析（绑定→默认回退）、签名、三段式卡片、模板渲染。"""

from __future__ import annotations

import httpx
import pytest
import respx

from alert_executor.config import FeishuBotChannel, NotifyChannelConfig, NotifyConfig
from alert_executor.feishu import (FeishuNotifier, _format_details, build_card,
                                   gen_sign, render_template)
from alert_executor.models import EvaluationResult, STATUS_OK
from conftest import make_rule

HOOK_DEFAULT = "https://open.feishu.cn/hook/default"
HOOK_BOUND = "https://open.feishu.cn/hook/bound"


def make_notifier(client: httpx.AsyncClient,
                  default_secret_ref: str = "DEFAULT_HOOK",
                  frontend_base_url: str | None = None) -> FeishuNotifier:
    return FeishuNotifier(client, NotifyConfig(
        default_channel=FeishuBotChannel(secret_ref=default_secret_ref),
        dashboard_url="https://grafana.example.com",
        frontend_base_url=frontend_base_url,
    ))


def hit_result() -> EvaluationResult:
    return EvaluationResult(status=STATUS_OK, hit=True, total_count=3, details="d")


def _fake_dt():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def test_gen_sign_deterministic():
    assert gen_sign("secret", 1700000000) == gen_sign("secret", 1700000000)
    assert gen_sign("secret", 1700000000) != gen_sign("secret", 1700000001)


def test_build_card_with_buttons():
    buttons = [{"tag": "button", "text": {"tag": "plain_text", "content": "事件详情"},
                "type": "default", "url": "https://fe/events/1"}]
    card = build_card("[告警] t", "red", ["line1"], buttons)
    action = card["card"]["elements"][-1]
    assert action["tag"] == "action"
    assert action["actions"][0]["url"] == "https://fe/events/1"


def test_build_card_without_buttons_has_no_action():
    card = build_card("[告警] t", "red", ["line"])
    assert not any(el["tag"] == "action" for el in card["card"]["elements"])


class TestFormatDetails:
    def test_str_passthrough_clipped(self):
        assert _format_details("ch-text") == "ch-text"

    def test_vector_samples_as_bold_lines(self):
        details = [{"metric": {"instance": "n1", "job": "node"}, "value": "91.2"}]
        out = _format_details(details)
        assert "instance=n1 job=node → **91.2**" in out

    def test_sample_limit_20_and_truncation_note(self):
        details = [{"metric": {"i": str(i)}, "value": str(i)} for i in range(25)]
        out = _format_details(details)
        assert out.count("**") == 40  # 20 行 × 2 个 **
        assert "其余 5 条序列略" in out

    def test_800_char_clip(self):
        details = [{"metric": {"pad": "x" * 100}, "value": "v"} for _ in range(20)]
        assert len(_format_details(details)) <= 808  # 800 + 截断标记余量

    def test_none_is_empty(self):
        assert _format_details(None) == ""


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

    @respx.mock
    async def test_event_detail_button_and_vector_trigger_values(self, monkeypatch):
        """夜莺对齐：回报拿 event_id → 事件详情按钮；vector 逐样本触发值加粗。"""
        monkeypatch.setenv("DEFAULT_HOOK", HOOK_DEFAULT)
        route = respx.post(HOOK_DEFAULT).respond(json={"code": 0})
        rule = make_rule(grafana_url=None)             # 仅事件详情按钮（无看板链接）
        result = EvaluationResult(status=STATUS_OK, hit=True, total_count=1,
                                  details=[{"metric": {"instance": "n1"},
                                            "value": "91.2"}])
        async with httpx.AsyncClient() as client:
            notifier = make_notifier(client,
                                     frontend_base_url="https://bingops.example.com/")
            await notifier.send_alert(rule, result, _fake_dt(), _fake_dt(),
                                      event_id=42)
        sent = route.calls[0].request.content.decode()
        assert "https://bingops.example.com/alerts/events/42" in sent  # 事件详情按钮
        assert "instance=n1 → **91.2**" in sent                        # 夜莺触发值逐样本
        assert "查看曲线" in sent                                      # dashboard_url 兜底按钮共存


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

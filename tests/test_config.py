"""bootstrap 配置加载与校验（规则/数据源走平台拉取，不在本地 config）。"""

from __future__ import annotations

import pytest

from alert_executor.config import (AGENT_CONFIG_PATH, AppConfig, PlatformConfig,
                                   load_config)
from conftest import make_rule, make_source


def test_load_bootstrap_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
platform:
  base_url: http://bingops.internal:8000/
  token_ref: T
  fetch_interval_seconds: 15
notify:
  default_channel:
    type: feishu-bot
    secret_ref: FEISHU_WEBHOOK_URL
  dashboard_url: https://grafana.example.com
evaluation:
  timeout_seconds: 8
  per_source_concurrency: 4
""",
        encoding="utf-8",
    )
    app = load_config(cfg)
    # base_url 尾斜杠规范化；拉取/回报 URL 由 base_url 派生
    assert app.platform.agent_config_url == f"http://bingops.internal:8000{AGENT_CONFIG_PATH}"
    assert app.platform.webhook_url == "http://bingops.internal:8000/api/v1/alerts/webhook"
    assert app.platform.fetch_interval_seconds == 15
    assert app.evaluation.per_source_concurrency == 4
    assert app.notify.default_channel.secret_ref == "FEISHU_WEBHOOK_URL"


def test_bootstrap_requires_platform(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("notify:\n  default_channel:\n    secret_ref: X\n", encoding="utf-8")
    with pytest.raises(Exception):  # noqa: B017,PT011 - platform 缺失应校验失败
        load_config(cfg)


def test_platform_defaults():
    p = PlatformConfig(base_url="http://b.internal", token_ref="T")
    assert p.timeout_seconds == 5.0
    assert p.fetch_interval_seconds == 30.0


def test_remote_rule_defaults():
    """平台缺省字段落内置默认（语义对齐旧 defaults）。"""
    rule = make_rule()
    assert rule.eval_interval_seconds == 60
    assert rule.threshold == 1
    assert rule.for_rounds == 1
    assert rule.severity == 2
    assert rule.stale_minutes == 5
    assert rule.enabled is True


def test_remote_source_type_restricted():
    with pytest.raises(Exception):  # noqa: B017,PT011 - type 枚举约束
        make_source(type_="mysql")

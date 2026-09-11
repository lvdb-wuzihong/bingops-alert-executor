"""bootstrap 配置加载与校验（pydantic v2）。

告警规则与数据源**从 bingops 平台拉取**（2026-09-10 用户决策，提前实现设计文档 §12
分发模式；设计文档 §3「执行器继续 config.yaml 自持规则」作废）。本地 config.yaml
只存 bootstrap：平台地址、agent token 引用、飞书渠道、评估参数。

``RuleConfig`` / ``DataSourceConfig`` 是平台 ``GET /api/v1/agent/config`` 下发内容的
校验模型；远端字段缺省时落内置默认，凭据只带 ``password_ref`` 引用名（红线 4）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, HttpUrl, model_validator

from .credentials import NO_AUTH

AGENT_CONFIG_PATH = "/api/v1/alerts/agent/config"


class PlatformConfig(BaseModel):
    """bingops 平台连接：拉规则 + 报事件共用同一 base_url 与 token。"""

    base_url: HttpUrl
    token_ref: str
    timeout_seconds: float = 5.0
    # 拉取节流：规则变更生效延迟 ≤ 一个拉取周期（§12）；版本协商使空轮廉价
    fetch_interval_seconds: float = Field(default=30.0, ge=1)

    @property
    def agent_config_url(self) -> str:
        return f"{str(self.base_url).rstrip('/')}{AGENT_CONFIG_PATH}"

    @property
    def webhook_url(self) -> str:
        return f"{str(self.base_url).rstrip('/')}/api/v1/alerts/webhook"


class FeishuBotChannel(BaseModel):
    """飞书机器人渠道（平台分发或 bootstrap 默认）。"""

    type: Literal["feishu-bot"] = "feishu-bot"
    # 凭据红线：secret_ref 是 env 变量名，值 = 完整 webhook 地址（URL 即 secret，平台不落真地址）
    secret_ref: str


class NotifyConfig(BaseModel):
    """执行器侧通知配置：仅默认渠道（规则未绑定平台渠道时使用）。"""

    default_channel: FeishuBotChannel
    # 卡片跳转兑底（规则未带 grafana_url 时）
    dashboard_url: str | None = None


class NotifyChannelConfig(BaseModel):
    """平台「通知渠道」登记的分发形态：平台只存引用名，发送动作在执行器。"""

    name: str
    type: Literal["feishu-bot"] = "feishu-bot"
    secret_ref: str
    # 非敏感附加参数（如 @ 手机号）；一期保留字段，不参与渲染
    extra_params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class EvaluationConfig(BaseModel):
    timeout_seconds: float = 10.0
    per_source_concurrency: int = 16


class DataSourceConfig(BaseModel):
    name: str
    type: Literal["clickhouse", "victoria", "prometheus"]
    # clickhouse
    host: str | None = None
    port: int = 8443
    secure: bool = True
    database: str = "default"
    username: str = "alert_ro"
    # victoria / prometheus
    url: HttpUrl | None = None
    # 凭据红线：只存引用名，运行时 resolve_credential 解引用
    password_ref: str = NO_AUTH

    @model_validator(mode="after")
    def _check_fields(self) -> "DataSourceConfig":
        if self.type == "clickhouse":
            if not self.host:
                raise ValueError(f"数据源 {self.name!r}: clickhouse 需要 host")
        else:
            if not self.url:
                raise ValueError(f"数据源 {self.name!r}: {self.type} 需要 url")
        return self

    def normalized_url(self) -> str:
        # HttpUrl 规范化后带尾斜杠，rstrip 防止 //api/v1/query 双斜杠
        return str(self.url).rstrip("/")


class RuleConfig(BaseModel):
    """平台下发的单条启用规则（§12「规则字段语义表」+ 平台「新增规则」表单）。"""

    code: str
    name: str
    # webhook payload 的 source，透传平台；存量规则对齐 ck-log-alert
    source: str = "ck-log-alert"
    # None = webhook-only 规则（事件由来源系统直接回报，执行器不评估）
    data_source: str | None = None
    eval_sql: str = ""
    # 以下字段平台缺省时落内置默认
    eval_interval_seconds: int = Field(default=60, ge=1)
    interval_minutes: int = Field(default=1, ge=1)
    threshold: int = Field(default=1, ge=0)
    for_rounds: int = Field(default=1, ge=1)
    severity: int = Field(default=2, ge=1, le=3)
    notify_interval_minutes: float = Field(default=0, ge=0)
    stale_minutes: int = Field(default=5, ge=1)
    # details 摘要最大条数（PromQL vector 摘要；平台分发字段）
    detail_limit: int = Field(default=10, ge=1)
    labels: dict[str, str] = Field(default_factory=dict)
    # 平台侧补齐的 labels（执行器规则未带时使用；labels 优先）
    static_labels: dict[str, str] = Field(default_factory=dict)
    # 绑定平台通知渠道名；None = 执行器默认渠道
    notify_channel: str | None = None
    # 卡片内毫秒级跳转地址
    grafana_url: str | None = None
    # 飞书卡片模板 JSON，随分发体下发，执行器自行渲染发送
    card_template: dict[str, Any] | None = None
    # 平台「仅记录」开关：False 时只评估回报 webhook，不发飞书
    notify_enabled: bool = True
    enabled: bool = True


class AppConfig(BaseModel):
    platform: PlatformConfig
    notify: NotifyConfig
    evaluation: EvaluationConfig = EvaluationConfig()


def load_config(path: str | Path) -> AppConfig:
    """加载 bootstrap config.yaml（不含规则；规则走平台拉取）。"""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"配置文件 {path} 不是有效的 YAML mapping")
    return AppConfig.model_validate(raw)

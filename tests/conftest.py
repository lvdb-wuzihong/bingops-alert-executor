"""测试公共 helper：快速构造规则/数据源（测试直接 `from conftest import`）。"""

from __future__ import annotations

from alert_executor.config import DataSourceConfig, RuleConfig


def make_rule(code: str = "r1", **kw) -> RuleConfig:
    """规则字段均有内置默认（平台下发缺省时同语义）。"""
    return RuleConfig(code=code, name=f"规则 {code}", data_source=kw.pop("data_source", "ds1"),
                      eval_sql=kw.pop("eval_sql", "SELECT 1, 'x'"), **kw)


def make_source(name: str = "ds1", type_: str = "clickhouse", **kw) -> DataSourceConfig:
    fields = {"name": name, "type": type_, "password_ref": "NO_AUTH"}
    fields.update(kw)
    if type_ == "clickhouse":
        fields.setdefault("host", "ch.internal")
    else:
        fields.setdefault("url", "http://vm.internal:8428")
    return DataSourceConfig(**fields)

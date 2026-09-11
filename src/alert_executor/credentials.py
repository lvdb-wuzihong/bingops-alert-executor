"""凭据解引用（设计文档 §0 决策 8 红线 / §12 取值约定）。

配置只存 ``*_ref`` 引用名（env 变量名），运行时从环境解引用；
``NO_AUTH`` 表示数据源无认证，连接时不带凭据。
"""

from __future__ import annotations

import os

NO_AUTH = "NO_AUTH"


def resolve_credential(ref: str) -> str | None:
    """按约定解析凭据引用。

    - ``NO_AUTH`` → ``None``（连接时不带认证参数）
    - 其他值视为 env 变量名，缺失则 fail fast，不静默降级。
    """
    if ref == NO_AUTH:
        return None
    try:
        return os.environ[ref]
    except KeyError:
        raise RuntimeError(
            f"凭据 env {ref!r} 未设置（fail fast，不静默降级）；"
            f"若数据源确无认证请将 *_ref 配置为 {NO_AUTH}"
        ) from None

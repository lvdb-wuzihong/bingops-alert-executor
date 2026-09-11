"""执行器运行时模型（非持久化）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 评估产出状态：评估成功得到判定结果为 ok；查询异常/超时/契约违反为 error。
# 执行器永不产生 resolved（恢复推导归平台，设计文档 §0 决策 4）。
STATUS_OK = "ok"
STATUS_ERROR = "error"

# webhook 回报状态
REPORT_FIRING = "firing"
REPORT_ERROR = "error"


@dataclass
class EvaluationResult:
    """单条规则单轮评估结果。"""

    status: str
    hit: bool = False
    total_count: int = 0
    details: Any = None
    error: str | None = None
    error_kind: str | None = field(default=None)

    @classmethod
    def from_error(cls, error: BaseException) -> "EvaluationResult":
        return cls(
            status=STATUS_ERROR,
            error=f"{type(error).__name__}: {error}",
            error_kind=type(error).__name__,
        )

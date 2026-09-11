"""BingOps 告警事件闭环执行器。

三步节拍循环：拉配置（一期本地 config.yaml）→ 评估到期规则（双评估器）→ 回报 webhook + 发飞书。
设计文档：docs/monitoring-design.md；开发纪律见 .qoder/skills/alert-executor-dev。
"""

__version__ = "0.1.0"

# BingOps 监控模块设计（告警事件闭环层）

> 状态：设计稿，待确认（2026-09-06）
> 范围：**一期 = webhook 事件闭环 + 告警事件表 + 统计**；监控源注册 / 规则分发挂起至二期，不废弃
> 上游输入：`docs/task-system-design.md`（控制面/执行面分离模式）、`/api/v1/sd/*`（机器对机器契约豁免先例）
> 事实基座：多 VPC VictoriaMetrics、夜莺（指标告警）、ck-log-alert（CK 日志告警，独立 GitLab 项目）、Grafana（查询）、ClickHouse（日志/tracing 存储）

## 0. 已锁定决策

| # | 决策 | 结论 |
|---|------|------|
| 1 | 平台定位 | bingops 只做监控**控制面 + 告警事件闭环层**（无状态）；不存指标采样、不做评估循环、不做查询代理 |
| 2 | 评估引擎归属 | **夜莺退役（2026-09-09 决策）**，夜莺与 ck-log-alert 均只作实现参考；评估统一由独立执行器（alert-executor）承担——双评估器：clickhouse（SQL 契约）+ victoria/prometheus（PromQL 表达式自带比较，vector 非空即触发）。评估循环不进 bingops 主进程 |
| 3 | 事件收敛 | 所有来源的告警 → bingops `POST /api/v1/alerts/webhook` 唯一入口；统一事件表，统计才有全局意义 |
| 4 | resolved 双轨语义 | 夜莺自带恢复事件直传；ck-log-alert **永不报恢复**（低于阈值静默跳过，保持现状），平台按规则级 `stale_minutes` 超时推导 resolved |
| 5 | 契约豁免 | webhook 为机器对机器接口：裸响应不套 `{"code","message","data"}` 统一信封外的强约束（见 §4.2 折中），不绑用户权限码，`X-Agent-Token` 静态鉴权 + 同 VPC 网络隔离 |
| 6 | 主旁路纪律 | 执行器回报失败只打日志，**绝不影响飞书通知主路**；平台不可用 = 统计降级，通知不降级 |
| 7 | details 黑盒 | 事件明细为 JSONB 自由结构，平台不解析内部字段；ck-log-alert 内部实现可整体变动不破契约 |
| 8 | 凭据红线（二期） | 监控源注册表只存 `password_ref` 引用名 + 非敏感连接参数；凭据留执行器侧 env（同 job-dispatch 只带 `ssh_key_ref` 纪律） |
| 9 | 依赖边界 | ck-log-alert 维持独立仓库、不并入 bingops 主仓库——clickhouse 驱动不进控制面镜像 |
| 10 | 设计校准 | 闭环层骨架对齐夜莺（事件表/状态机/屏蔽/通知组）与 WatchAlert（值班/升级/降噪）成熟设计；吸收项与不搬清单见 §14，不引入其评估引擎形态 |

---

## 1. 现状拓扑（事实基座）

```
各 VPC:  vmagent ──http_sd──→ bingops /sd/v1/nodes（按 vpc/region/model_codes 过滤，已有）
              └─remote_write→ 本 VPC VictoriaMetrics ──→ Grafana（查询/看板）
集中:    日志/tracing ──→ ClickHouse

演进（2026-09-09）：夜莺与 ck-log-alert 进入退役计划，评估统一由 alert-executor 承担
（迁移过渡期双跑对账，详见 §11/§12）。
```

- CMDB 的 SD labels（`provider/region/zone/vpc/env/app/hostname`）是资源 ↔ 指标 ↔ 告警的统一 join key。
- 缺口不在评估层（夜莺 + ck-log-alert 已覆盖），而在：告警事件无统一落库、无恢复状态机、无工单/值班联动、无跨来源统计。

## 2. 总体架构：评估引擎可以多，闭环层只有一个

```
┌─ 评估执行面（alert-executor，独立进程；夜莺/ck-log-alert 退役后由它统一承担）─────┐
│  PromQL 评估器（victoria/prometheus 源）    SQL 评估器（clickhouse 源）           │
│  表达式自带比较，vector 非空即触发           聚合 SQL，单行两列契约                  │
│  拉分发体 → 评估 → 发飞书（渠道 secret_ref）→ 回报事件 ─┤← 主路，平台旁路故障不影响  │
└──────────────────────────┼─────────────────────────────────────┘
                           │ webhook（firing/resolved/error）
                           ▼
        bingops POST /api/v1/alerts/webhook（X-Agent-Token）
                 └─ alert_events 表（uq_active_firing 幂等合并）
                      ├─ stale 扫描（后台循环 60s，幂等 UPDATE）
                      ├─ 首次 firing → 规则映射处理组 → 自动开单 + tier1 值班派单
                      ├─ resolved（事件直传或 stale 超时）→ 工单流转
                      └─ 统计 API
```

**纪律**：bingops 不评估告警规则；执行器不写平台表（只调 webhook）；at-least-once 靠活跃 firing 唯一约束幂等合并；状态机只有 DB 一个事实源，进程重启不丢。

## 3. 一期范围与落位

| 做 | 不做（挂起至二期） |
|----|--------------------|
| webhook 端点 + 静态 token | 监控源注册表 |
| alert_events / alert_rules 表（v21 迁移） | 规则分发 API（执行器继续 config.yaml 自持规则） |
| firing/resolved 状态机 + stale 扫描 | notify 协议强制化（一期响应体已带 `notify`，执行器可选接入） |
| 处理组映射 → 自动开单 + 派单 | 规则评估 SQL 入库 |
| 统计 API + 事件列表 API | 前端页面 |

目录落位（平铺 alert 域，规模未到建子包的程度）：

```
bingops/models/alert.py            # AlertEvent / AlertRule ORM
bingops/schemas/alert.py           # WebhookPayload / 统计响应
bingops/repositories/alert_repo.py
bingops/services/alert_service.py  # 状态机 + 资源匹配 + 开单编排
bingops/api/v1/alerts.py           # webhook + 事件列表 + 统计
sql/migrations/v21_alert_events.sql
```

## 4. Webhook 契约

### 4.1 请求体

```json
{
  "source": "ck-log-alert",
  "rule_code": "java-error-prod",
  "rule_name": "生产环境 Java ERROR 告警",
  "status": "firing",
  "window_start": "2026-09-06T20:29:01+08:00",
  "window_end": "2026-09-06T20:30:01+08:00",
  "total_count": 1,
  "severity": 2,
  "labels": {"env": "prod", "app": "product-soa"},
  "details": "[Pod]: musem-app-... （文本块或结构化数组，平台不解析）",
  "error": null
}
```

| 字段 | 类型 | 约束 |
|------|------|------|
| `source` | str | 枚举：`ck-log-alert` \| `n9e`，后续来源在此扩充 |
| `rule_code` | str | 稳定聚合键，执行器与平台 `alert_rules.code` 对齐；变更须两侧同步（一期人工纪律，二期分发后自然消除） |
| `status` | str | `firing` \| `resolved` \| `error`；ck-log-alert 一期只用 firing/error，`resolved` 仅供夜莺直传 |
| `window_*` | datetime | ISO8601 带时区，平台统一转 UTC 存；`resolved` 事件可缺省 |
| `total_count` | int | firing 语义必填 |
| `severity` | int | 可选，对齐夜莺值域：1=严重 2=中等 3=轻微；缺省取规则表 `default_severity`；开单时映射工单优先级 |
| `labels` | object | 尽力而为，CMDB 关联输入（§8），缺省 `{}` |
| `details` | any | JSONB 黑盒（决策 7） |
| `error` | str | `status=error` 时的异常摘要 |

### 4.2 响应体

```json
{"code": 0, "message": "ok", "data": {"event_id": 123, "notify": true, "suppress_reason": null, "repeat_after_minutes": 30}}
```

`notify` 语义：`false` = 同一活跃 firing 本平台已处理过，执行器可跳过本轮飞书通知；**回报失败 / 超时 / 响应不可解析时执行器默认发**——主路自动退化为现状行为，宁多勿漏。一期执行器可不读该字段（行为不变），二期模块化时转正。

`repeat_after_minutes`：夜莺 repeat notify 同款——活跃 firing 持续期间，距上次通知超过该间隔时响应 `notify:true` 重发提醒（防遗忘），未超时返回 `notify:false`。一期协议预留，执行器可选读。

### 4.3 错误语义与幂等

- `401`（token 错）/ `422`（schema 错）：执行器打 error 日志，**不重试**——契约/配置错，重试无意义。
- `5xx` / 超时 / 连接失败：打 warning，本轮放弃；下轮评估若仍达标自然重报（at-least-once 由重报实现）。
- 平台幂等：`(source, rule_code)` 上活跃 firing 唯一约束（§5.1），重复 firing 合并更新 `last_seen_at / total_count / details`，不开新单。

## 5. 数据模型

### 5.1 alert_events

```sql
CREATE TABLE alert_events (
    id             BIGSERIAL PRIMARY KEY,
    source         VARCHAR(32)  NOT NULL,
    rule_code      VARCHAR(128) NOT NULL,
    rule_name      VARCHAR(255),
    status         VARCHAR(16)  NOT NULL,              -- firing | resolved | error
    window_start   TIMESTAMPTZ,
    window_end     TIMESTAMPTZ,
    first_seen_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_seen_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    resolved_at    TIMESTAMPTZ,
    resolve_reason VARCHAR(32),                        -- resolved_event | stale_timeout
    total_count    BIGINT       NOT NULL DEFAULT 0,
    severity       SMALLINT     NOT NULL DEFAULT 2,              -- 对齐夜莺：1严重 2中等 3轻微
    labels         JSONB        NOT NULL DEFAULT '{}',
    resource_ids   JSONB        NOT NULL DEFAULT '[]',      -- CMDB 尽力匹配（§8）
    details        JSONB,
    monitoring_source_id BIGINT,                            -- 数据源归属（v24，防同名规则跨源吞没）
    error          TEXT,
    ticket_id      BIGINT,                             -- 逻辑引用 tickets.id
    group_id       BIGINT,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_alert_active_firing ON alert_events (source, rule_code, COALESCE(monitoring_source_id, 0)) WHERE status = 'firing';
CREATE INDEX idx_alert_events_status_time ON alert_events (status, first_seen_at);
CREATE INDEX idx_alert_events_last_seen ON alert_events (last_seen_at) WHERE status = 'firing';
CREATE INDEX idx_alert_events_labels ON alert_events USING gin (labels);
```

### 5.2 alert_rules（平台侧元数据，一期非分发源）

```sql
CREATE TABLE alert_rules (
    id             BIGSERIAL PRIMARY KEY,
    source         VARCHAR(32)  NOT NULL,
    code           VARCHAR(128) NOT NULL,              -- 对齐执行器 rule_code
    name           VARCHAR(255),
    group_id       BIGINT,                             -- 开单处理组
    stale_minutes  INT NOT NULL DEFAULT 5,             -- 建议 2~3 × 执行器 interval
    static_labels  JSONB NOT NULL DEFAULT '{}',        -- 平台侧补齐 labels（执行器不带时）
    notify_enabled BOOLEAN NOT NULL DEFAULT TRUE,      -- false = 只记录不开单
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_alert_rules_source_code ON alert_rules (source, code);
```

一期它是「开单映射的配置源」（哪条规则派给哪个组、多久算恢复）；二期加 `eval_sql / threshold / feishu_template` 等字段后升级为分发源，表不改。

**未知 rule_code 的行为**：事件照常落表（统计完整性优先），开单跳过并打 warning——新规则上线漏配映射的故障模式是「有数无单」，可观测、可后补，而不是 500。

## 6. 事件状态机

```
firing 回报 → 活跃 firing 存在?
   ├─ 否 → 建事件 → notify_enabled? → 自动开单 + tier1 派单
   └─ 是 → 更新 last_seen_at / total_count / details（不重复开单）
resolved 回报（夜莺）→ 置 resolved（resolve_reason=resolved_event）→ 工单流转
stale 扫描（60s 周期）→ 活跃 firing 且 last_seen_at 超过 stale_minutes
                      → 置 resolved（resolve_reason=stale_timeout）→ 工单流转
error 回报 → 独立落行（status='error'），不进状态机、不开单，单列统计
```

- stale 扫描实现为 startup 后台任务（`asyncio.create_task` 定时循环，同 `snapshot_sweep_loop` 先例）：纯 DB 驱动的幂等 UPDATE，无内存状态，多副本部署无害——不破坏无状态纪律（决策 1）。
- 防抖（夜莺 `for_duration` 同款）：规则配 `for_rounds`（连续 M 轮评估达标才报 firing），**在执行器侧实现**——平台状态机无 pending 态，比 n9e 更简；偶发抖动不进事件表。
- 事件表只存「闭环需要」的数据，不复制夜莺/ck-log-alert 的全量历史；原始明细留在 `details` 与各自系统。

## 7. 工单 / 值班联动

> **全局总闸 `BINGOPS_ALERT_TICKET_ENABLED`（当前默认 false，用户决策：先不关联）**：关闭时告警只落事件与状态机，不创建/流转任何工单（`_try_open_ticket` / `_after_resolved` 直接短路）；开启后仍受规则级 `notify_enabled` 细粒度控制。

- 首次 firing：按 `alert_rules.group_id` 找处理组 → 复用现有自动派单链路（当日值班 tier1 轮转优先，回退组成员轮转）自动开单；标题 `[告警] {rule_name}`，内容带时间窗、total_count、details 摘要、source/rule_code。
- 同一活跃 firing 生命周期内**只开一张单**（唯一约束保证）；resolved 时工单按工单状态机流转至 resolved。
- `notify_enabled=false` 的规则只落事件不开单（低噪规则白名单）。

## 8. CMDB 资源关联（尽力匹配，不阻塞）

`labels` 中有什么匹配什么：`hostname` → 主机资源 name；`pod`/`pod_name` → k8s pod 资源 name；`app`/`env` → 标签体系。命中即把 resource_ids 记入事件（为二期资源维度统计打底）；全不中留空数组，**不阻塞开单、不 500**。匹配实现收敛在 `alert_service._match_resources` 单点，随 CMDB 演进只改一处。

## 9. 统计 API

- `GET /api/v1/alerts/events?status=&source=&rule_code=&since=&until=` 分页列表
- `GET /api/v1/alerts/stats/summary?since=&until=&group_by=source|rule_code|group_id|day`
  - 各 status 事件数、当前活跃 firing 数、平均恢复时长 `avg(resolved_at - first_seen_at)`
- 高频 firing 规则排名即噪声规则治理清单。权限码按 RBAC 规范注册（建议 `alert:read` / `alert:manage`）。

## 10. ck-log-alert 适配清单（约 +30 行）

| 脚本现有 | payload 字段 | 说明 |
|---|---|---|
| `rule["name"]` | `rule_name` | 原样 |
| （无）| `rule_code` | config 每条规则新增 `code:` 一行 |
| `error_count` | `total_count` | |
| `now-interval ~ now` | `window_start/end` | ISO8601 带 `+08:00` |
| `log_details` 文本块 | `details` | 原样塞入 |
| 查询失败分支 | `status: "error"` + `error` | 不发飞书的那套改动不要，两路都走 |
| `threshold` 判定、SQL、卡片 | 不动 | 低于阈值静默跳过 = 恢复推导的输入（决策 4） |

回报函数骨架（requests 已在依赖中）：

```python
def report_event(cfg: dict, rule: dict, status: str, total: int,
                 win_start: datetime, win_end: datetime, details, err: str | None = None):
    try:
        requests.post(
            cfg["bingops"]["webhook_url"],
            json={"source": "ck-log-alert", "rule_code": rule["code"], "rule_name": rule["name"],
                  "status": status, "window_start": win_start.isoformat(),
                  "window_end": win_end.isoformat(), "total_count": total,
                  "labels": rule.get("labels", {}), "details": details, "error": err},
            headers={"X-Agent-Token": cfg["bingops"]["token"]}, timeout=5,
        ).raise_for_status()
    except Exception as e:
        logger.warning("回报 bingops 失败（不影响飞书主路）: %s", e)
```

顺带修复隐患：`interval_minutes` 默认值两处不一致（`build_query` 默认 1 / `run()` 默认 5），统一为一个默认值。

## 11. 夜莺侧接入（迁移过渡期）

> **演进更新（2026-09-09）：夜莺进入退役计划**。本节仅适用于过渡期：指标规则逐条迁至平台（PromQL 原样贴入 eval_sql，绑 victoria 数据源），执行器接管后夜莺侧同步禁用对应规则，双跑对账；清零后夜莺下线，指标告警由执行器 PromQL 评估器承担（vector 非空即触发）。

通知媒介追加 webhook 指向同一端点（`source: "n9e"`），恢复事件直传 `status: "resolved"`；映射表里为涉及规则配 `group_id`。零代码，纯配置。

## 12. 终态：数据源管理 + 规则配置在平台，评估在执行面

与 runner（任务执行面）、cmdb-informer/cloud-syncer（同步执行面）完全同构：控制面管资产与闭环，执行面无业务逻辑。告警域分工：

| 能力 | 位置 | 说明 |
|------|------|------|
| 数据源管理 | bingops（类 Grafana/夜莺体验） | type（clickhouse / victoria / prometheus）、名称、非敏感连接参数、`password_ref`（决策 8 红线） |
| 告警规则配置 | bingops UI | eval_sql / threshold / 窗口 / stale_minutes / 处理组，绑定数据源 |
| 评估执行 | 独立执行器（**新建项目 alert-executor**，双评估器：clickhouse SQL 契约 error_count+log_details〔参考 ck-log-alert：去重聚合/三段式卡片/Grafana 毫秒跳转〕+ victoria prometheus PromQL〔表达式自带比较，vector 非空即触发，夜莺退役后接管指标〕；独立仓库，决策 9） | 进程内节拍循环：拉规则 + 源引用 → 按源类型选评估器 → 报事件 + 发飞书 |
| 事件闭环 | bingops | webhook / 状态机 / 工单 / 统计（本文 §4~§9，一期交付） |

**调度与锁语义（刻意无分布式锁）**：
- 调度层：执行器单实例节拍循环，天然单点（VPC 分片暂不启用——见部署边界简化）；将来多执行器时规则按数据源静态归属分片；
- 执行层：单条规则重叠保护（评估耗时超过间隔时 flock 跳过本轮）；
- 平台层：无需锁，事件幂等靠 `uq_active_firing` 唯一约束兜底。

仅当「同一数据源多执行器实例 HA」成为真实需求时才引入 lease，当前不预设。

**部署与网络边界**（2026-09-10 用户简化：**VPC 不作为管理/调度维度，monitoring_sources 的 region/vpc 字段已删除**——单执行器公网加白出口 IP 即可访问全部数据源；各 VPC vmagent 跨 VPC 走公网访问平台端点 + 安全组源 IP 白名单，executor 沿用同一通道模式）：
- **单实例 executor**：一个 Deployment（bingops 同 VPC）按数据源注册表公网访问各 VPC 的 CH/VM，安全组源 IP 限定 executor 出口（VPC 内 K8s 经 NAT 网关出口 IP 固定，白名单可维护）——与 vmagent→平台完全同构；
- **公网通道加固条件（上线前必做）**：CH 禁用 default 账号、executor 用专用只读账号（仅 SELECT 限定库表）+ TLS；VM 侧 TLS + 鉴权（vmauth 或反代）；数据源端口安全组仅对 executor 出口 IP 开放；残余风险 = 凭据强度，与 vmagent 通道一致，不新增安全等级；
- **执行器永不直连平台 PG**：与控制面只走两个出站 HTTP（拉规则+源引用、报事件），凭据引用在执行器侧 env 解——runner「不写业务表」同款纪律，执行面经 API 契约通信、不共享数据库；executor→bingops API 同样走 X-Agent-Token + 平台侧白名单；
- **运行形态**：Deployment 单副本 + 进程内节拍循环（每轮：拉配置 → 评估到期规则 → 回报），优于 CronJob（无每分钟 Pod 冷启动）；进程内循环属执行器本职，不违反「评估循环不进 bingops 主进程」约束；重叠保护用进程内 flock；**规则级扫描间隔 `eval_interval_seconds`（秒，默认 60）由执行器本地调度（next_due = last_eval + interval），与查询窗口 `interval_minutes` 独立**；

**规则字段语义表（两类规则的字段职责，录入时对照）**：

| 字段 | CH 日志规则 | VM 指标规则 |
|---|---|---|
| `eval_interval_seconds` | 扫描间隔（秒，多久评一次） | 同左 |
| `interval_minutes` | 查询窗口（SQL `{window_minutes}` 占位） | 不参与（窗口写进 PromQL `[5m]`） |
| `threshold` | 判定阈值（error_count ≥ threshold） | 不参与（比较写进表达式） |
| `for_rounds` | 连续 M 轮达标才报 firing（防抖） | 同左（vector 非空计轮） |
| `stale_minutes` | 恢复推导窗口（建议 2~3× 扫描间隔） | 同左 |
| `eval_sql` | 聚合 SQL（单行两列 error_count+log_details） | PromQL 表达式（自带比较） |

- **拉取方向恒为执行器→平台**：执行器零入站端口，平台零 agent 状态（无注册/心跳/存活管理），故障自愈靠下一轮重拉；规则变更生效延迟 ≤ 一个调度周期；
- **演进路径**：将来建 CEN/对等连接内网互通后通道整体收进内网（vmagent→平台与 executor→数据源一并受益）；CH/VM 实例数增长到单实例管理不动时，按数据源静态归属分片为多执行器（契约不变）。

二期实施：**ck-log-alert 仅作逻辑参考，不改造**；分发 API 按执行器 agent 身份（静态 token，复用 X-Agent-Token）拉取启用规则 + 源引用（只带引用不带凭据），eval_sql 契约为单行两列 error_count + log_details（存量 SQL 原样可贴）；`config.yaml` 退役；notify 协议转正；stale 超时可被执行器真实 resolved 事件取代；新增 `alert_mutes` 屏蔽表（labels 匹配 + 时间窗，与变更封禁窗口联动）与 firing 升级策略（持续未响应升级通知，WatchAlert escalation 同款）。卡片模板作为配置数据进 alert_rules（执行器拉取后自行渲染发送），**通知仍由执行器发送**，平台不建通知媒介（三期再议）。

**凭据取值约定（执行器侧实现）**：`password_ref` / `secret_ref` 是执行器侧 env 变量名，取值 `os.environ[ref]`；约定值 **`NO_AUTH`** = 数据源无认证，连接时不带凭据（仅限内网/白名单可达，加认证后改为 env 变量名）：

```python
def resolve_credential(ref: str) -> str | None:
    if ref == "NO_AUTH":
        return None          # 不带认证参数
    return os.environ[ref]   # 缺失则 fail fast，不静默降级
```

三期触发条件（评估循环是否内聚进平台再议）：多 VPC 多执行器实例化 / 规则数显著增长 / 执行器已薄至「拉取 → 评估 → 回报」三步。届时成本 = 三大件（评估调度器 + 多副本协调、数据源查询客户端、飞书通知媒介进平台），收益 = 少维护一个执行器组件；故障域耦合（平台发版 = 告警盲窗）是永久代价，由真实数据权衡。

### 万级演进预留（10k 规则前的工程化清单，2026-09-10 评审后定稿）

**采纳项**：
- **分发版本协商**：执行器带版本号拉 `agent/config`，规则无变化返回空体（避免万级每轮 10-20MB 全量传输）；
- **执行器并发评估池**：asyncio Semaphore（16-32，按数据源分组限流）+ 每条评估硬超时（5-10s）——万级下串行必死；
- **舱壁隔离**：静态分片天然支持（CH 慢查询只影响 CH 执行器），进程内 PromQL/SQL 双池叠加；
- **stale 扫描批量更新**：万级活跃 firing 时分批 LIMIT（避免长事务）；扫描输入改纯 SQL 差集判断（少拉内存）；
- **事件保留期清理（Retention）**：resolved 事件保留 90 天，后台任务定期清理（复用 stale sweep 模式）。

**拒绝项（记录理由，防止将来被同类建议带偏）**：
- **Webhook 异步化（MQ/Redis Stream 缓冲）**：①「行锁竞争」是误解——部分唯一索引下不同规则的 INSERT 是不同行、完全并行，数千 INSERT/min 对 PG 是轻负载；②致命伤：notify 同步语义（响应体抑制判断）依赖同步处理，异步化会把通知状态逼回执行器，击穿「状态全在平台」设计。风暴场景的正解是渠道级聚合（Top N + 剩余计数），不是 MQ；
- **一致性哈希分片（worker_id/total_workers）**：HPA 扩缩容时 N 变化 → 规则漂移 → for_rounds 计数失效 → 防抖抖动；动态共识复杂度违背无锁设计。静态归属分片（分片单位=数据源）更简更稳；
- **防抖状态入 Redis**：计数丢失的后果是漏报几轮（方向安全：告警重启宁漏勿误），非误报；为可容忍代价引入 Redis 依赖不划算。「推给 PromQL FOR 语法」不可实现——FOR 是 rule engine 概念，裸 query 无此语义，必须执行器自维护（即 for_rounds）。

**采纳项（2026-09-10 第二轮评审补充）**：
- **唯一索引加数据源维度**：`uq_alert_active_firing` 改为 `(source, rule_code, COALESCE(monitoring_source_id, 0))`——修复平台原生规则（source 固定 bingops）跨数据源同名 code 互相吞没的正确性 bug；事件归属取规则绑定的数据源；
- **error 顺延推导窗口**：评估失败（error 回报）时顺延活跃 firing 的 `last_seen_at`——评估失败=状态未知，保守保持告警，防数据源断连导致的 stale 假恢复风暴（优于「扫描时查 error 子查询」的冻结方案，实现更简）；
- **执行器退化模式 RateLimiter**：平台不可用退化直发飞书时，同规则本地最小发送间隔（15min），防平台重启/发版期间飞书被刷屏（执行器实现职责）；
- **labels GIN 索引**：支撑 app/env/hostname 的 JSONB 检索（零成本，表空期加入）。

**第二轮部分拒绝**：webhook 同步临界区缩小（asyncio.Queue 解耦 CMDB 匹配/开单）——场景认知有误：CMDB 匹配是同库 IN 查询（毫秒级）而非「RPC 1~2s」，工单总闸默认关闭；进程内 Queue 还引入重启丢开单的交付语义问题。将来总闸开启+规模上去后再议「开单异步化」。

## 13. 实施顺序

1. v21 迁移 + ORM/Repo/Schema
2. `alert_service` 状态机 + webhook 端点 + stale 扫描后台任务
3. 事件列表 + 统计 API
4. ck-log-alert `report_event()` + `code` 字段 + interval 默认值修复
5. 夜莺通知媒介配 webhook
6. 观察一周（统计校准噪声规则）→ 启动二期

---

## 14. 设计校准记录：与夜莺 / WatchAlert 对照（2026-09-07）

闭环层骨架与夜莺同构（规则→评估→事件→屏蔽→通知→恢复），按决策 10 吸收以下设计，拒绝其评估引擎形态：

| 来源概念 | 我们的实现 | 归属 |
|---|---|---|
| AlertRule.for_duration 防抖 / pending | 规则 `for_rounds`（连续 M 轮达标才报 firing），执行器侧实现，平台无 pending 态 | 执行器侧，二期字段化 |
| severity 1/2/3 | payload 可选 + alert_events.severity + 工单优先级映射 | 一期 |
| repeat notify | 响应体 `repeat_after_minutes` 活跃 firing 重发提醒 | 一期协议预留 |
| AlertMute 屏蔽 | `alert_mutes`（labels 匹配 + 时间窗），与变更封禁窗口联动 | 二期 |
| v8 告警规则/通知规则分离 | alert_rules 检测字段与通知字段分组演进 | 二期表结构 |
| WatchAlert escalation | firing 持续未响应升级通知 | 二期候选 |
| Cur/HisEvent 分表 | 单表 + status + `uq_active_firing` 部分索引（等价更简） | 不搬 |
| 多租户 / 订阅 / 通知媒介体系 / 平台内嵌评估引擎 | — | 不搬（评估与通知在执行器；夜莺退役后指标评估并入执行器 PromQL 评估器，仍不进平台进程） |

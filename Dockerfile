# bingops alert-executor 容器镜像
# 运行形态（设计文档 §12）：单副本 Deployment + 进程内节拍循环，零入站端口；
# 凭据红线：镜像不打包任何凭据，password_ref/secret_ref 的实际值全部运行时 env 注入。
#
# 构建：docker build -t alert-executor:latest .
# 运行：docker run --rm \
#         -e BINGOPS_AGENT_TOKEN=xxx \
#         -e FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxx \
#         -v $(pwd)/config.yaml:/etc/alert-executor/config.yaml:ro \
#         alert-executor:latest

FROM python:3.13-slim AS builder

WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
# 装到独立 prefix，运行期仅拷贝产物（无 pip/构建工具链）
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.13-slim

LABEL org.opencontainers.image.title="bingops-alert-executor" \
      org.opencontainers.image.description="BingOps 告警事件闭环执行器：双评估器 + 飞书通知 + webhook 回报" \
      org.opencontainers.image.source="https://git.internal/bingops/alert-executor"

# tzdata：告警时间窗按本地时区（Asia/Shanghai）渲染；平台统一转 UTC 存
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ALERT_EXECUTOR_HEARTBEAT=/tmp/alert-executor-heartbeat

# 非 root 运行；心跳文件目录授予写权限
RUN useradd --system --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /etc/alert-executor /tmp/alert-executor
COPY --from=builder /install /usr/local
RUN chown -R appuser:appuser /tmp/alert-executor

USER appuser
WORKDIR /home/appuser

# 零入站端口：健康检查用 exec 检查心跳文件新鲜度（>90s 未更新视为不健康）
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os,time,sys; p=os.environ.get('ALERT_EXECUTOR_HEARTBEAT','/tmp/alert-executor-heartbeat'); sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 90 else 1)"]

# 配置走挂载（K8s ConfigMap / 本地 -v），bootstrap 只含平台地址与 *_ref 引用名
ENTRYPOINT ["python", "-m", "alert_executor", "-c", "/etc/alert-executor/config.yaml"]

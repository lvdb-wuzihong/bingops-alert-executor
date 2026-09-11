"""python -m alert_executor [-c config.yaml]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import load_config
from .scheduler import AlertExecutor

# 文件日志（可选）：env LOG_FILE 设置路径时，stdout 之外额外写轮转文件，
# 供 vector file source tail 采集（50MB × 5 个备份）；未设置则仅 stdout（容器化默认）。
# 路径支持 {hostname} 占位符（K8s 自动注入 Pod 名）——多副本共用 hostPath 目录时
# 各写各的文件，避免双进程写同一文件。
LOG_FILE_ENV = "LOG_FILE"
LOG_MAX_BYTES = 50 * 1024 * 1024
LOG_BACKUP_COUNT = 5


def _setup_logging(verbose: bool) -> None:
    """stdout 恒有（容器标准采集点）；LOG_FILE 有值时额外挂轮转文件。

    文件不可写（权限/路径）时降级为仅 stdout 并打警告——日志故障不阻断启动。
    """
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    log_file = os.environ.get(LOG_FILE_ENV)
    if log_file:
        log_file = log_file.replace("{hostname}",
                                    os.environ.get("HOSTNAME", "local"))
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            handlers.append(RotatingFileHandler(
                log_file, maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
            ))
        except OSError as e:  # 权限/路径问题：降级为仅 stdout，不阻断启动
            print(f"[alert-executor] 日志文件不可用({e})，降级为仅 stdout")

    # force=True：清掉既有 handler 重挂（进程内唯一配置者；也避免测试环境
    # pytest logging 插件预挂 capture handler 导致 basicConfig 静默 no-op）
    logging.basicConfig(level=level, format=fmt, handlers=handlers, force=True)


def main() -> None:
    parser = argparse.ArgumentParser(prog="alert-executor",
                                     description="BingOps 告警事件闭环执行器")
    parser.add_argument("-c", "--config", default="config.yaml",
                        help="配置文件路径（默认 ./config.yaml）")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="输出 debug 日志")
    args = parser.parse_args()

    _setup_logging(args.verbose)
    cfg = load_config(args.config)
    executor = AlertExecutor(cfg)
    try:
        asyncio.run(executor.run())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("收到退出信号，执行器停止")


if __name__ == "__main__":
    main()

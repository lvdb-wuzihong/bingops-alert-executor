"""python -m alert_executor [-c config.yaml]"""

from __future__ import annotations

import argparse
import asyncio
import logging

from .config import load_config
from .scheduler import AlertExecutor


def main() -> None:
    parser = argparse.ArgumentParser(prog="alert-executor",
                                     description="BingOps 告警事件闭环执行器")
    parser.add_argument("-c", "--config", default="config.yaml",
                        help="配置文件路径（默认 ./config.yaml）")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="输出 debug 日志")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    cfg = load_config(args.config)
    executor = AlertExecutor(cfg)
    try:
        asyncio.run(executor.run())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("收到退出信号，执行器停止")


if __name__ == "__main__":
    main()

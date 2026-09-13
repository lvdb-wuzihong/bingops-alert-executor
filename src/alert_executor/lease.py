"""Redis 租约协调（Deployment 多副本，2026-09-13 用户决策：引入 Redis）。

语义：每条规则在 Redis 持有租约 ``lease:{source}/{code} = instance_id``，
TTL = max(60s, 2×评估间隔)：
- 空闲 → ``SET NX PX`` 抢占，本实例评估；
- 已是自己的租约 → 续租，继续评估（下一轮再抢）；
- 别人的 → 跳过（该实例存活中）；
- 实例死亡 → 租约到期自动被其他实例接管（自愈，无需清理逻辑）；
- **Redis 不可用 → fail-open 全量评估**：宁多勿漏（平台幂等合并 + 节流兜底），
  恢复后租约按 NX 重新竞争。
instance_id 用 POD_NAME（Deployment 注入随机名，天然唯一）或 uuid4。
"""

from __future__ import annotations

import logging
import os
import uuid

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from .state import RateLimiter

logger = logging.getLogger(__name__)

LEASE_PREFIX = "alert-executor:lease:"
NOTIFY_PREFIX = "alert-executor:notify:"


class RedisLeaseCoordinator:
    def __init__(self, url: str, instance_id: str | None = None,
                 client: "aioredis.Redis | None" = None) -> None:
        self._instance_id = instance_id or os.environ.get("POD_NAME") or uuid.uuid4().hex
        self._redis = client if client is not None else aioredis.from_url(
            url, decode_responses=True)

    async def acquire(self, key: str, ttl_seconds: float) -> bool:
        """尝试获取/续租规则租约。返回 True = 本实例应评估该规则。"""
        lease_key = LEASE_PREFIX + key
        ttl_ms = int(ttl_seconds * 1000)
        try:
            acquired = await self._redis.set(
                lease_key, self._instance_id, nx=True, px=ttl_ms)
            if acquired:
                return True
            holder = await self._redis.get(lease_key)
            if holder == self._instance_id:
                await self._redis.pexpire(lease_key, ttl_ms)  # 续租
                return True
            return False
        except RedisError as e:
            logger.warning("Redis 租约不可用，fail-open 全量评估（平台节流兜底）: %s", e)
            return True

    async def close(self) -> None:
        try:
            await self._redis.aclose()
        except RedisError:
            pass


class SharedRateLimiter:
    """跨副本共享的通知限流（多副本去重的执行器侧实现）。

    平台对流水型事件（recorded/error）每轮回 notify=true，若每个副本各自用
    本地内存限流，N 副本 = N 张卡。本类把「上次发卡」窗口键放进 Redis，
    两副本共享同一状态：同窗口内只有第一个到的副本发卡。
    Redis 不可用 → 降级本地内存限流（单副本语义，多副本下临时回到 N 倍）。
    """

    def __init__(self, url: str | None = None,
                 client: "aioredis.Redis | None" = None,
                 local: RateLimiter | None = None) -> None:
        self._redis = (client if client is not None
                       else (aioredis.from_url(url, decode_responses=True)
                             if url else None))
        self._local = local or RateLimiter()

    async def allow(self, key: str, window_seconds: float) -> bool:
        """同 key 在 window_seconds 内只放行一次（跨副本共享）。"""
        window_ms = int(window_seconds * 1000)
        if self._redis is not None:
            try:
                acquired = await self._redis.set(
                    NOTIFY_PREFIX + key, "1", nx=True, px=window_ms)
                return bool(acquired)
            except RedisError as e:
                logger.warning("Redis 共享限流不可用，降级本地限流: %s", e)
                self._redis = None  # 降级为本地，避免每轮重复报错
        return self._local.allow(key, window_seconds)


__all__ = ["RedisLeaseCoordinator", "SharedRateLimiter"]

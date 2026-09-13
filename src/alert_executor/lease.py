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

logger = logging.getLogger(__name__)

LEASE_PREFIX = "alert-executor:lease:"


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


__all__ = ["RedisLeaseCoordinator"]

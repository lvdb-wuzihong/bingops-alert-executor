"""Redis 租约协调：NX 抢占、续租、他人持有拒绝、Redis 异常 fail-open。"""

from __future__ import annotations

import pytest
from redis.exceptions import RedisError

from alert_executor.lease import RedisLeaseCoordinator


class FakeRedis:
    """内存版 set/get/pexpire，模拟 NX 语义；fail=True 时抛 RedisError。"""

    def __init__(self, fail: bool = False) -> None:
        self.store: dict[str, str] = {}
        self.fail = fail

    async def set(self, key, value, nx=False, px=None):
        if self.fail:
            raise RedisError("redis down")
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key):
        if self.fail:
            raise RedisError("redis down")
        return self.store.get(key)

    async def pexpire(self, key, ms):
        if self.fail:
            raise RedisError("redis down")


def make_coordinator(client: FakeRedis, instance_id: str = "pod-a"):
    return RedisLeaseCoordinator("redis://localhost:6379/0",
                                 instance_id=instance_id, client=client)


@pytest.mark.asyncio
async def test_idle_lease_acquired():
    client = FakeRedis()
    coord = make_coordinator(client)
    assert await coord.acquire("bingops/r1", ttl_seconds=60) is True
    assert client.store["alert-executor:lease:bingops/r1"] == "pod-a"


@pytest.mark.asyncio
async def test_holder_renews():
    client = FakeRedis()
    coord = make_coordinator(client, instance_id="pod-a")
    assert await coord.acquire("bingops/r1", 60) is True
    assert await coord.acquire("bingops/r1", 60) is True  # 自己续租仍成功


@pytest.mark.asyncio
async def test_other_holder_rejected():
    client = FakeRedis()
    coord_a = make_coordinator(client, instance_id="pod-a")
    coord_b = make_coordinator(client, instance_id="pod-b")
    assert await coord_a.acquire("bingops/r1", 60) is True
    assert await coord_b.acquire("bingops/r1", 60) is False  # pod-a 持有


@pytest.mark.asyncio
async def test_lease_taken_over_after_expiry():
    """实例死亡 → 租约到期（Fake 里模拟删除）→ 其他实例接管。"""
    client = FakeRedis()
    coord_a = make_coordinator(client, instance_id="pod-a")
    coord_b = make_coordinator(client, instance_id="pod-b")
    await coord_a.acquire("bingops/r1", 60)
    del client.store["alert-executor:lease:bingops/r1"]  # 模拟 TTL 过期
    assert await coord_b.acquire("bingops/r1", 60) is True


@pytest.mark.asyncio
async def test_redis_down_fails_open():
    """Redis 不可用 → fail-open 放行（宁多勿漏，平台节流兜底），不抛异常。"""
    coord = make_coordinator(FakeRedis(fail=True))
    assert await coord.acquire("bingops/r1", 60) is True


@pytest.mark.asyncio
async def test_instance_id_from_pod_name_env(monkeypatch):
    monkeypatch.setenv("POD_NAME", "alert-executor-7fd9-abc")
    client = FakeRedis()
    coord = RedisLeaseCoordinator("redis://localhost/0", client=client)
    await coord.acquire("k", 60)
    assert client.store["alert-executor:lease:k"].startswith("alert-executor-7fd9")

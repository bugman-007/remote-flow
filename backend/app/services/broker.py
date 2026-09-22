"""Redis-backed broker with an in-process fallback.

Used for: SSE fan-out (pub/sub + capped streams), login rate limiting, provider
semaphores/token buckets and settings caches. Everything degrades to an
in-memory implementation when REDIS_URL is unset or unreachable, which is what
the test suite and `make dev` (single process) use.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from typing import Any, Protocol

from app.config import get_settings


class Broker(Protocol):
    async def publish(self, channel: str, payload: dict) -> None: ...
    async def subscribe(self, channel: str) -> AsyncIterator[dict]: ...
    async def append_stream(self, stream: str, payload: dict, *, max_length: int = 500) -> str: ...
    async def read_stream(self, stream: str, *, after_id: str | None = None, limit: int = 500) -> list[tuple[str, dict]]: ...
    async def incr(self, key: str, *, ttl_seconds: int) -> int: ...
    async def get_int(self, key: str) -> int: ...
    async def delete(self, key: str) -> None: ...
    async def acquire_slot(self, key: str, *, limit: int, timeout_s: float) -> bool: ...
    async def release_slot(self, key: str) -> None: ...
    async def slots_in_use(self, key: str) -> int: ...
    async def health(self) -> bool: ...
    async def close(self) -> None: ...


# ------------------------------------------------------------------- in-memory


class MemoryBroker:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._streams: dict[str, deque[tuple[str, dict]]] = defaultdict(lambda: deque(maxlen=500))
        self._counters: dict[str, tuple[int, float | None]] = {}
        self._slots: dict[str, deque[float]] = defaultdict(deque)
        self._stream_seq = 0
        self._lock = asyncio.Lock()

    async def publish(self, channel: str, payload: dict) -> None:
        for queue in list(self._subscribers.get(channel, ())):
            queue.put_nowait(payload)

    async def subscribe(self, channel: str) -> AsyncIterator[dict]:  # type: ignore[override]
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers[channel].add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers[channel].discard(queue)

    async def append_stream(self, stream: str, payload: dict, *, max_length: int = 500) -> str:
        async with self._lock:
            self._stream_seq += 1
            entry_id = f"{int(time.time() * 1000)}-{self._stream_seq}"
            target = self._streams[stream]
            target.append((entry_id, payload))
            while len(target) > max_length:
                target.popleft()
        return entry_id

    async def read_stream(self, stream: str, *, after_id: str | None = None, limit: int = 500) -> list[tuple[str, dict]]:
        entries = list(self._streams.get(stream, ()))
        if after_id:
            index = next((i for i, (entry_id, _) in enumerate(entries) if entry_id == after_id), -1)
            entries = entries[index + 1 :]
        return entries[-limit:]

    async def incr(self, key: str, *, ttl_seconds: int) -> int:
        value, expires = self._counters.get(key, (0, None))
        now = time.time()
        if expires and expires < now:
            value = 0
        value += 1
        self._counters[key] = (value, now + ttl_seconds)
        return value

    async def get_int(self, key: str) -> int:
        value, expires = self._counters.get(key, (0, None))
        if expires and expires < time.time():
            return 0
        return value

    async def delete(self, key: str) -> None:
        self._counters.pop(key, None)

    #: Mirrors the Redis broker: a slot marker left behind by a crashed worker
    #: expires, so a killed process can never wedge the provider concurrency.
    SLOT_TTL_SECONDS = 30.0

    async def acquire_slot(self, key: str, *, limit: int, timeout_s: float) -> bool:
        deadline = time.time() + timeout_s
        while True:
            bucket = self._slots[key]
            now = time.time()
            while bucket and bucket[0] <= now:
                bucket.popleft()
            if len(bucket) < limit:
                bucket.append(now + self.SLOT_TTL_SECONDS)
                return True
            if time.time() >= deadline:
                return False
            await asyncio.sleep(0.05)

    async def release_slot(self, key: str) -> None:
        bucket = self._slots.get(key)
        now = time.time()
        while bucket and bucket[0] <= now:
            bucket.popleft()
        if bucket:
            bucket.pop()

    async def slots_in_use(self, key: str) -> int:
        return len(self._slots.get(key, ()))

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        return None


# ----------------------------------------------------------------------- redis


class RedisBroker:
    def __init__(self, client) -> None:
        self._client = client

    async def publish(self, channel: str, payload: dict) -> None:
        await self._client.publish(channel, json.dumps(payload, default=str))

    async def subscribe(self, channel: str) -> AsyncIterator[dict]:  # type: ignore[override]
        pubsub = self._client.pubsub()
        await pubsub.subscribe(channel)
        try:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15.0)
                if message is None:
                    yield {"type": "keepalive"}
                    continue
                data = message.get("data")
                if isinstance(data, bytes):
                    try:
                        yield json.loads(data)
                    except json.JSONDecodeError:
                        continue
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    async def append_stream(self, stream: str, payload: dict, *, max_length: int = 500) -> str:
        return await self._client.xadd(stream, {"data": json.dumps(payload, default=str)}, maxlen=max_length, approximate=False)

    async def read_stream(self, stream: str, *, after_id: str | None = None, limit: int = 500) -> list[tuple[str, dict]]:
        start = f"({after_id}" if after_id else "-"
        rows = await self._client.xrange(stream, min=start, max="+", count=limit)
        out = []
        for entry_id, fields in rows:
            raw = fields.get(b"data") or fields.get("data")
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                out.append((entry_id, json.loads(raw)))
            except (TypeError, json.JSONDecodeError):
                continue
        return out

    async def incr(self, key: str, *, ttl_seconds: int) -> int:
        value = await self._client.incr(key)
        if value == 1:
            await self._client.expire(key, ttl_seconds)
        return int(value)

    async def get_int(self, key: str) -> int:
        value = await self._client.get(key)
        return int(value or 0)

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def acquire_slot(self, key: str, *, limit: int, timeout_s: float) -> bool:
        deadline = time.time() + timeout_s
        while True:
            now = time.time()
            pipe = self._client.pipeline()
            pipe.zremrangebyscore(key, "-inf", now)
            pipe.zadd(key, {f"{now}:{id(self)}": now + 30})
            pipe.zcard(key)
            _, _, count = await pipe.execute()
            if int(count) <= limit:
                return True
            await self._client.zrem(key, f"{now}:{id(self)}")
            if time.time() >= deadline:
                return False
            await asyncio.sleep(0.1)

    async def release_slot(self, key: str) -> None:
        # The slot marker self-expires after 30 s; nothing to do explicitly.
        return None

    async def slots_in_use(self, key: str) -> int:
        await self._client.zremrangebyscore(key, "-inf", time.time())
        return int(await self._client.zcard(key))

    async def health(self) -> bool:
        try:
            await self._client.ping()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def close(self) -> None:
        await self._client.aclose()


_broker: Broker | None = None


def get_broker() -> Broker:
    global _broker
    if _broker is not None:
        return _broker
    url = get_settings().redis_url
    if url:
        try:
            import redis.asyncio as redis_asyncio

            client = redis_asyncio.from_url(url, encoding="utf-8", decode_responses=False)
            _broker = RedisBroker(client)
            return _broker
        except Exception:  # noqa: BLE001 - fall back to memory
            pass
    _broker = MemoryBroker()
    return _broker


def set_broker(broker: Broker | None) -> None:
    global _broker
    _broker = broker


def event_channel(user_id: str) -> str:
    return f"rf:events:{user_id}"


def event_stream(user_id: str) -> str:
    return f"rf:stream:{user_id}"

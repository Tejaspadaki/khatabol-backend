"""In-process pub/sub that pushes announcements to the merchant app / Soundbox over WebSocket.

Single-process only; swap for Redis pub/sub when running more than one worker.
"""

import asyncio
import uuid
from collections import defaultdict
from datetime import datetime, timezone


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[uuid.UUID, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, merchant_id: uuid.UUID) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers[merchant_id].add(q)
        return q

    def unsubscribe(self, merchant_id: uuid.UUID, q: asyncio.Queue) -> None:
        self._subscribers[merchant_id].discard(q)

    def publish(self, merchant_id: uuid.UUID, event_type: str, text: str, **data) -> dict:
        event = {
            "type": event_type,
            "text": text,
            "at": datetime.now(timezone.utc).isoformat(),
            **data,
        }
        for q in list(self._subscribers[merchant_id]):
            if not q.full():
                q.put_nowait(event)
        return event


bus = EventBus()

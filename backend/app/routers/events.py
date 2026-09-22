"""Server-Sent Events stream (RT-1…RT-8)."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.deps import current_user
from app.models import User
from app.services import events as events_service
from app.services.broker import event_channel, get_broker

router = APIRouter(tags=["events"])


def _format(envelope: dict) -> str:
    return (
        f"id: {envelope.get('id')}\n"
        f"event: {envelope.get('type')}\n"
        f"data: {json.dumps(envelope.get('data') or {})}\n\n"
    )


@router.get("/events")
async def stream_events(request: Request, user: User = Depends(current_user)):
    from app.db import session_scope

    last_event_id = request.headers.get("last-event-id")
    backlog: list[dict] = []
    if last_event_id:
        async with session_scope() as session:
            backlog = await events_service.replay(session, user.id, last_event_id=last_event_id)
    broker = get_broker()

    async def generator():
        try:
            for envelope in backlog:
                yield _format(envelope)
            yield ": connected\n\n"
            async for envelope in broker.subscribe(event_channel(user.id)):
                if await request.is_disconnected():
                    break
                if envelope.get("type") == "keepalive":
                    yield ": keepalive\n\n"
                    continue
                yield _format(envelope)
        except asyncio.CancelledError:  # client went away
            return

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

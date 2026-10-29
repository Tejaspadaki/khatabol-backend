import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.events import bus

router = APIRouter()


@router.websocket("/ws/merchants/{merchant_id}/events")
async def merchant_events(ws: WebSocket, merchant_id: uuid.UUID):
    """Live announcements for the merchant app / Soundbox: UPI auto-settles, unknown payers, summaries."""
    await ws.accept()
    q = bus.subscribe(merchant_id)
    try:
        while True:
            await ws.send_json(await q.get())
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(merchant_id, q)

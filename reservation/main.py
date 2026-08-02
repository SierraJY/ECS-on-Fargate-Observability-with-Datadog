import asyncio
import os

import httpx
from ddtrace import tracer
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

INVENTORY_URL = os.getenv("INVENTORY_URL", "http://inventory:8000")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment:8000")
NOTIFICATION_URL = os.getenv("NOTIFICATION_URL", "http://notification:8000")

CHARGE_AMOUNT = 10000

app = FastAPI()


class ReservationBody(BaseModel):
    seat_id: str
    user_id: str


class CancelBody(BaseModel):
    user_id: str


def _set_span_tags(*, error: bool = False, **tags):
    span = tracer.current_span()
    if span is None:
        return
    for key, value in tags.items():
        span.set_tag(key, value)
    if error:
        span.error = 1


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/seats")
async def list_seats():
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{INVENTORY_URL}/seats")
    return resp.json()


@app.post("/reservations")
async def create_reservation(body: ReservationBody):
    _set_span_tags(**{"seat_id": body.seat_id, "usr.id": body.user_id})

    async with httpx.AsyncClient(timeout=10.0) as client:
        lock_resp, charge_resp = await asyncio.gather(
            client.post(
                f"{INVENTORY_URL}/seats/{body.seat_id}/lock",
                json={"locked_by": body.user_id},
            ),
            client.post(
                f"{PAYMENT_URL}/charge",
                json={"user_id": body.user_id, "amount": CHARGE_AMOUNT},
            ),
            return_exceptions=True,
        )

        lock_ok = isinstance(lock_resp, httpx.Response) and lock_resp.status_code == 200
        charge_ok = isinstance(charge_resp, httpx.Response) and charge_resp.status_code == 200

        if lock_ok and charge_ok:
            confirm_resp = await client.post(f"{INVENTORY_URL}/seats/{body.seat_id}/confirm")
            if confirm_resp.status_code != 200:
                await client.post(f"{INVENTORY_URL}/seats/{body.seat_id}/release")
                _set_span_tags(
                    error=True,
                    **{"failure.stage": "confirm", "failure.reason": "confirm_failed"},
                )
                raise HTTPException(
                    status_code=502,
                    detail="failed to confirm seat after successful lock and charge",
                )

            await client.post(
                f"{NOTIFICATION_URL}/notify",
                json={"user_id": body.user_id, "message": f"seat {body.seat_id} booked"},
            )
            return {"status": "booked", "seat_id": body.seat_id, "user_id": body.user_id}

        failed_parts = []
        stages = []
        if not lock_ok:
            failed_parts.append("inventory")
            stages.append("lock")
        if not charge_ok:
            failed_parts.append("payment")
            stages.append("charge")

        if lock_ok:
            await client.post(f"{INVENTORY_URL}/seats/{body.seat_id}/release")

        _set_span_tags(
            error=True,
            **{
                "failure.stage": ",".join(stages),
                "failure.reason": ",".join(f"{part}_failed" for part in failed_parts),
            },
        )

        raise HTTPException(
            status_code=409,
            detail=f"reservation failed: {', '.join(failed_parts)} failed",
        )


@app.post("/reservations/{seat_id}/cancel")
async def cancel_reservation(seat_id: str, body: CancelBody):
    _set_span_tags(**{"seat_id": seat_id, "usr.id": body.user_id})

    async with httpx.AsyncClient(timeout=10.0) as client:
        cancel_resp = await client.post(f"{INVENTORY_URL}/seats/{seat_id}/cancel")
        if cancel_resp.status_code != 200:
            _set_span_tags(
                error=True,
                **{"failure.stage": "cancel", "failure.reason": "not_booked"},
            )
            raise HTTPException(
                status_code=409, detail=f"cancel failed: seat {seat_id} is not booked"
            )

        await client.post(
            f"{NOTIFICATION_URL}/notify",
            json={"user_id": body.user_id, "message": f"seat {seat_id} cancelled"},
        )
        return {"status": "cancelled", "seat_id": seat_id, "user_id": body.user_id}

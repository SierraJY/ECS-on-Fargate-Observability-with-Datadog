import asyncio
import os

import httpx
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


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/reservations")
async def create_reservation(body: ReservationBody):
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
        if not lock_ok:
            failed_parts.append("inventory")
        if not charge_ok:
            failed_parts.append("payment")

        if lock_ok:
            await client.post(f"{INVENTORY_URL}/seats/{body.seat_id}/release")

        raise HTTPException(
            status_code=409,
            detail=f"reservation failed: {', '.join(failed_parts)} failed",
        )

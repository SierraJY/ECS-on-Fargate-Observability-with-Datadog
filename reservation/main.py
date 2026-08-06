import asyncio
import logging
import os

import httpx
from ddtrace import tracer
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] "
    "[dd.service=%(dd.service)s dd.env=%(dd.env)s dd.version=%(dd.version)s "
    "dd.trace_id=%(dd.trace_id)s dd.span_id=%(dd.span_id)s] - %(message)s",
)
logger = logging.getLogger("reservation")

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
                logger.error(
                    "failed to confirm seat after successful lock and charge",
                    extra={"seat_id": body.seat_id, "user_id": body.user_id},
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
            **{
                "failure.stage": ",".join(stages),
                "failure.reason": ",".join(f"{part}_failed" for part in failed_parts),
            },
        )
        logger.warning(
            "reservation failed",
            extra={
                "seat_id": body.seat_id,
                "user_id": body.user_id,
                "failed_parts": failed_parts,
            },
        )

        if not charge_ok:
            # Payment(다운스트림 서비스) 장애 — 리소스 충돌이 아니라 업스트림 실패이므로 502.
            # ddtrace가 5xx는 자동으로 span.error를 마킹하므로 수동 태깅 불필요.
            raise HTTPException(
                status_code=502,
                detail=f"reservation failed: {', '.join(failed_parts)} failed",
            )

        # charge_ok가 True인데 여기 도달했다면 lock만 실패한 것 — 결제 시점보다 먼저
        # 다른 사용자가 결제를 완료해 좌석이 이미 잠겼거나(BOOKED) 팔린 경우.
        raise HTTPException(
            status_code=409,
            detail="이미 판매된 좌석입니다.",
        )


@app.post("/reservations/{seat_id}/cancel")
async def cancel_reservation(seat_id: str, body: CancelBody):
    _set_span_tags(**{"seat_id": seat_id, "usr.id": body.user_id})

    async with httpx.AsyncClient(timeout=10.0) as client:
        cancel_resp = await client.post(f"{INVENTORY_URL}/seats/{seat_id}/cancel")
        if cancel_resp.status_code != 200:
            _set_span_tags(
                **{"failure.stage": "cancel", "failure.reason": "not_booked"},
            )
            logger.warning(
                "cancel failed: seat is not booked",
                extra={"seat_id": seat_id, "user_id": body.user_id},
            )
            raise HTTPException(
                status_code=409, detail=f"cancel failed: seat {seat_id} is not booked"
            )

        await client.post(
            f"{NOTIFICATION_URL}/notify",
            json={"user_id": body.user_id, "message": f"seat {seat_id} cancelled"},
        )
        return {"status": "cancelled", "seat_id": seat_id, "user_id": body.user_id}

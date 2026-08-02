import os

import httpx
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

RESERVATION_URL = os.getenv("RESERVATION_URL", "http://reservation:8000")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ReservationBody(BaseModel):
    seat_id: str
    user_id: str


class CancelBody(BaseModel):
    user_id: str


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/seats")
async def list_seats():
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(f"{RESERVATION_URL}/seats")
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=502, detail=f"reservation service unreachable: {exc}"
            )
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
    )


@app.post("/reservations")
async def create_reservation(body: ReservationBody):
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(
                f"{RESERVATION_URL}/reservations", json=body.model_dump()
            )
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=502, detail=f"reservation service unreachable: {exc}"
            )
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
    )


@app.post("/reservations/{seat_id}/cancel")
async def cancel_reservation(seat_id: str, body: CancelBody):
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(
                f"{RESERVATION_URL}/reservations/{seat_id}/cancel",
                json=body.model_dump(),
            )
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=502, detail=f"reservation service unreachable: {exc}"
            )
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
    )

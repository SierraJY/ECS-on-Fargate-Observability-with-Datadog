import os
import ssl
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "inventory")
DB_USERNAME = os.getenv("DB_USERNAME", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")
DB_SSLMODE = os.getenv("DB_SSLMODE", "disable")  # disable | verify-full
DB_SSL_CA_PATH = os.getenv("DB_SSL_CA_PATH", "/app/global-bundle.pem")

SEAT_COUNT = 20


def _build_ssl_context():
    if DB_SSLMODE != "verify-full":
        return None
    return ssl.create_default_context(cafile=DB_SSL_CA_PATH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USERNAME,
        password=DB_PASSWORD,
        ssl=_build_ssl_context(),
    )
    async with app.state.pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seats (
                seat_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'AVAILABLE',
                locked_by TEXT,
                locked_at TIMESTAMPTZ
            )
            """
        )
        count = await conn.fetchval("SELECT COUNT(*) FROM seats")
        if count == 0:
            await conn.executemany(
                "INSERT INTO seats (seat_id) VALUES ($1)",
                [(f"A{i}",) for i in range(1, SEAT_COUNT + 1)],
            )
    yield
    await app.state.pool.close()


app = FastAPI(lifespan=lifespan)


class LockBody(BaseModel):
    locked_by: str | None = None


@app.get("/health")
async def health():
    try:
        async with app.state.pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok"}


@app.get("/seats")
async def list_seats():
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT seat_id, status, locked_by, locked_at FROM seats ORDER BY seat_id"
        )
    return [dict(row) for row in rows]


@app.post("/seats/{seat_id}/lock")
async def lock_seat(seat_id: str, body: LockBody):
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE seats SET status = 'LOCKED', locked_by = $1, locked_at = now()
            WHERE seat_id = $2 AND status = 'AVAILABLE'
            RETURNING seat_id, status
            """,
            body.locked_by,
            seat_id,
        )
    if row is None:
        raise HTTPException(status_code=409, detail=f"seat {seat_id} is not available")
    return dict(row)


@app.post("/seats/{seat_id}/confirm")
async def confirm_seat(seat_id: str):
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE seats SET status = 'BOOKED'
            WHERE seat_id = $1 AND status = 'LOCKED'
            RETURNING seat_id, status
            """,
            seat_id,
        )
    if row is None:
        raise HTTPException(status_code=409, detail=f"seat {seat_id} is not locked")
    return dict(row)


@app.post("/seats/{seat_id}/release")
async def release_seat(seat_id: str):
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE seats SET status = 'AVAILABLE', locked_by = NULL, locked_at = NULL
            WHERE seat_id = $1 AND status = 'LOCKED'
            RETURNING seat_id, status
            """,
            seat_id,
        )
    if row is None:
        raise HTTPException(status_code=409, detail=f"seat {seat_id} is not locked")
    return dict(row)


@app.post("/seats/{seat_id}/cancel")
async def cancel_seat(seat_id: str):
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE seats SET status = 'AVAILABLE', locked_by = NULL, locked_at = NULL
            WHERE seat_id = $1 AND status = 'BOOKED'
            RETURNING seat_id, status
            """,
            seat_id,
        )
    if row is None:
        raise HTTPException(status_code=409, detail=f"seat {seat_id} is not booked")
    return dict(row)

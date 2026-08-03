import asyncio
import random
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()

_chaos_state = {"mode": "off", "delay_ms": 0, "error_rate": 0.0}


class ChargeBody(BaseModel):
    user_id: str
    amount: float


class ChaosBody(BaseModel):
    mode: Literal["latency", "error", "off"]
    delay_ms: int = 0
    error_rate: float = 0.0


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/admin/chaos")
async def set_chaos(body: ChaosBody):
    _chaos_state["mode"] = body.mode
    _chaos_state["delay_ms"] = body.delay_ms
    _chaos_state["error_rate"] = body.error_rate
    return _chaos_state


@app.post("/charge")
async def charge(body: ChargeBody):
    if _chaos_state["mode"] == "latency":
        await asyncio.sleep(_chaos_state["delay_ms"] / 1000)
    elif _chaos_state["mode"] == "error":
        if random.random() < _chaos_state["error_rate"]:
            raise HTTPException(status_code=500, detail="payment gateway error (chaos injected)")

    return {"status": "charged", "user_id": body.user_id, "amount": body.amount}

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class ChargeBody(BaseModel):
    user_id: str
    amount: float


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/charge")
async def charge(body: ChargeBody):
    return {"status": "charged", "user_id": body.user_id, "amount": body.amount}

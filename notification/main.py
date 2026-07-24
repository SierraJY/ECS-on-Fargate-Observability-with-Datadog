import logging

from fastapi import FastAPI
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("notification")

app = FastAPI()


class NotifyBody(BaseModel):
    user_id: str
    message: str


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/notify")
async def notify(body: NotifyBody):
    logger.info("notification sent user_id=%s message=%s", body.user_id, body.message)
    return {"status": "sent", "user_id": body.user_id}

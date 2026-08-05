import logging

from fastapi import FastAPI
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] "
    "[dd.service=%(dd.service)s dd.env=%(dd.env)s dd.version=%(dd.version)s "
    "dd.trace_id=%(dd.trace_id)s dd.span_id=%(dd.span_id)s] - %(message)s",
)
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

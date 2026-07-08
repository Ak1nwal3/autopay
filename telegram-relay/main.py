import os

import httpx
from fastapi import FastAPI, Request, Response

app = FastAPI()

UPSTREAM_URL = os.environ["UPSTREAM_URL"]
SECRET_TOKEN = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

client = httpx.AsyncClient(timeout=10.0)


@app.post("/telegram/webhook")
async def relay(request: Request) -> Response:
    if SECRET_TOKEN:
        header = request.headers.get("x-telegram-bot-api-secret-token", "")
        if header != SECRET_TOKEN:
            return Response(status_code=401)

    body = await request.body()
    upstream = await client.post(
        UPSTREAM_URL,
        content=body,
        headers={"content-type": "application/json"},
    )
    return Response(content=upstream.content, status_code=upstream.status_code)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}

"""API fals pentru testarea simulatorului pana e gata backend-ul real.

Imita contractul: 202 la citire noua, 409 la duplicat (acelasi Idempotency-Key),
503 aleatoriu (MOCK_ERROR_RATE) ca sa se vada retry-urile.

  GET /stats -> cate citiri unice a primit, cate duplicate a respins, cate 503 a dat
"""

import os
import random

import uvicorn
from fastapi import FastAPI, Request, Response

ERROR_RATE = float(os.getenv("MOCK_ERROR_RATE", "0.05"))
PORT = int(os.getenv("MOCK_PORT", "9000"))

app = FastAPI()
seen: set[str] = set()
stats = {"accepted": 0, "duplicate_rejected": 0, "error_503": 0}


@app.post("/api/v1/ingest")
async def ingest(request: Request) -> Response:
    await request.body()
    if random.random() < ERROR_RATE:
        stats["error_503"] += 1
        return Response(status_code=503)
    key = request.headers.get("Idempotency-Key", "")
    if key in seen:
        stats["duplicate_rejected"] += 1
        return Response(status_code=409)
    seen.add(key)
    stats["accepted"] += 1
    return Response(status_code=202)


@app.get("/stats")
async def get_stats() -> dict:
    return {**stats, "unique_rows": len(seen)}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")

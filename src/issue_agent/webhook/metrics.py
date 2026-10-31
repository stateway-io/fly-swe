from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, Response
from prometheus_client import generate_latest
from prometheus_client.openmetrics import exposition as openmetrics

router = APIRouter()


@router.get("/metrics")
async def metrics() -> Response:
    return PlainTextResponse(
        content=generate_latest().decode("utf-8"),
        media_type=openmetrics.CONTENT_TYPE_LATEST,
    )

"""Prometheus 指标端点。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.metrics import render_metrics

router = APIRouter(tags=["observability"])


@router.get("/metrics")
def metrics(db: Session = Depends(get_db)) -> Response:
    return Response(
        content=render_metrics(db),
        media_type="text/plain; version=0.0.4; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )

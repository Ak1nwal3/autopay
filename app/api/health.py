"""Liveness and readiness probes.

- /healthz  liveness: process is up
- /readyz   readiness: dependencies (DB) are reachable
- /metrics  Prometheus scrape endpoint (no auth; protect via network policy)

The root path `/` is owned by the SPA in `app.main:serve_index` —
this router deliberately does not register a `/` route so the
SPA can take over without conflict.
"""
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlmodel import Session

from app.core.database import get_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness probe")
def healthz() -> dict:
    return {"status": "alive"}


@router.get("/readyz", summary="Readiness probe (checks DB)")
def readyz(session: Session = Depends(get_session)) -> JSONResponse:
    try:
        session.exec(text("SELECT 1")).one()
        return JSONResponse(
            status_code=200,
            content={"status": "ready", "database": "ok"},
        )
    except Exception as exc:  # noqa: BLE001
        # Log the full DB driver error for operators, but only return
        # a generic "unreachable" to the caller (probes shouldn't
        # leak DB internals).
        logger.exception("Readiness probe DB check failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"status": "not ready", "database": "unreachable"},
        )


@router.get("/metrics", summary="Prometheus metrics scrape endpoint")
def metrics() -> Response:
    """Exposes the registered `prometheus_client` collectors in
    text/plain exposition format. Scrape target for Prometheus.

    NOTE: this route is intentionally unauthenticated. In production,
    restrict access at the network layer (Kubernetes NetworkPolicy,
    firewall rules, or run the scraper as a sidecar). Do NOT expose
    `/metrics` to the public internet — it leaks request volume and
    business event rates.
    """
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

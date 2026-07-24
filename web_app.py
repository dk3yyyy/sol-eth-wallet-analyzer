import logging
import os
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from analyzer import analyze_wallet
from services import ServiceError

logger = logging.getLogger(__name__)
FRONTEND_DIST = Path(__file__).resolve().parent / "frontend" / "dist"


class SlidingWindowRateLimiter:
    def __init__(self, max_requests: int, window_seconds: int, max_clients: int = 10_000):
        if max_requests < 1 or window_seconds < 1 or max_clients < 1:
            raise ValueError("Rate limiter values must be positive integers")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_clients = max_clients
        self._requests: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: str, *, now: float | None = None) -> bool:
        timestamp = time.monotonic() if now is None else now
        cutoff = timestamp - self.window_seconds
        with self._lock:
            events = self._requests.get(key)
            if events is None:
                if len(self._requests) >= self.max_clients:
                    self._requests.popitem(last=False)
                events = deque()
                self._requests[key] = events
            else:
                self._requests.move_to_end(key)
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.max_requests:
                return False
            events.append(timestamp)
            return True


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


analyze_limiter = SlidingWindowRateLimiter(
    max_requests=_positive_int_env("ANALYZE_RATE_LIMIT", 20),
    window_seconds=_positive_int_env("ANALYZE_RATE_WINDOW_SECONDS", 60),
)

CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "base-uri 'none'",
        "connect-src 'self'",
        "font-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "img-src 'self' data:",
        "object-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
    )
)

app = FastAPI(
    title="Solana & Ethereum Wallet Analyzer",
    description="Read-only portfolio analysis for public blockchain addresses.",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str = Field(min_length=1, max_length=128)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if request.method == "POST" and request.url.path == "/api/analyze":
        client_key = request.client.host if request.client else "unknown"
        if not analyze_limiter.allow(client_key):
            response: Response = JSONResponse(
                status_code=429,
                content={"detail": "Analysis limit reached. Try again in a minute."},
                headers={"Retry-After": str(analyze_limiter.window_seconds)},
            )
        else:
            response = await call_next(request)
    else:
        response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=()"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "supported_chains": ["solana", "ethereum"],
    }


@app.post("/api/analyze")
async def analyze(payload: AnalyzeRequest):
    try:
        return await analyze_wallet(payload.address, force_refresh=False)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ServiceError as exc:
        logger.warning("Wallet data provider unavailable")
        raise HTTPException(
            status_code=503,
            detail="Wallet data providers are temporarily unavailable. Try again shortly.",
        ) from exc


if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")

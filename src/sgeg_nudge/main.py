"""AI Buddy — FastAPI application entry point.

Mounts:
  /admin    Admin dashboard (nudge audit, tickets, course/learner config)
  /lti      LTI 1.3 OIDC flow (login, launch, jwks, dev-launch)
  /chat     Student chat UI and REST API
  /static   Served static files (CSS, JS)

Run locally:
    uv run uvicorn sgeg_nudge.main:app --reload --port 8000

Dev launch (no Canvas required):
    open http://localhost:8000/lti/dev
    # or with custom student:
    open "http://localhost:8000/lti/dev?name=Amahle&course=Grade+10+English"
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from sgeg_nudge.admin import router as admin_router
from sgeg_nudge.chat import router as chat_router
from sgeg_nudge.dashboard import router as dashboard_router
from sgeg_nudge.db import init_engine
from sgeg_nudge.lti import router as lti_router
from sgeg_nudge.widget import router as widget_router

_STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    init_engine()

    app = FastAPI(
        title="AI Buddy",
        description="Canvas LTI chatbot for K-12 students, powered by Claude.",
        version="0.2.0",
    )

    # Trust proxy headers from ngrok / reverse proxies so request.base_url
    # returns the public HTTPS URL, not http://localhost:8000.
    # This fixes the LTI redirect_uri mismatch when running behind ngrok.
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request as StarletteRequest
    from starlette.types import ASGIApp, Receive, Scope, Send

    class ProxyFixMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # Rebuild scope with forwarded host/proto from ngrok headers
            forwarded_proto = request.headers.get("x-forwarded-proto")
            forwarded_host  = request.headers.get("x-forwarded-host") or \
                              request.headers.get("host", "")
            if forwarded_proto and forwarded_host:
                request.scope["scheme"] = forwarded_proto
                # Patch the server tuple so base_url uses the public host
                host_port = forwarded_host.split(":")
                host = host_port[0]
                port = int(host_port[1]) if len(host_port) > 1 else (443 if forwarded_proto == "https" else 80)
                request.scope["server"] = (host, port)
                request.scope["headers"] = [
                    (k, v) for k, v in request.scope["headers"]
                    if k.lower() not in (b"host",)
                ] + [(b"host", forwarded_host.encode())]
            return await call_next(request)

    app.add_middleware(ProxyFixMiddleware)

    # Static files (CSS, JS) served at /static/*
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    app.include_router(lti_router)
    app.include_router(chat_router)
    app.include_router(admin_router)
    app.include_router(dashboard_router)
    app.include_router(widget_router)

    @app.get("/", include_in_schema=False)
    def root():
        from fastapi.responses import JSONResponse
        return JSONResponse({
            "service":    "AI Buddy",
            "version":    "0.2.0",
            "chat":       "/chat?session=<session_id>",
            "dev_launch": "/lti/dev",
            "admin":      "/admin",
            "dashboard":  "/dashboard",
            "lti_jwks":   "/lti/jwks",
        })

    return app


app = create_app()

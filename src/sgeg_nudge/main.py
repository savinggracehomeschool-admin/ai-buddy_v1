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
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/dashboard/login", status_code=302)

    @app.exception_handler(404)
    async def not_found(request, exc):
        from fastapi.responses import HTMLResponse
        html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Page Not Found — SGEG Education Coach</title>
  <style>
    *{margin:0;padding:0;box-sizing:border-box}
    body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         background:#F0F4F7;display:flex;align-items:center;
         justify-content:center;min-height:100vh;color:#0A2240}
    .card{background:#fff;border-radius:12px;padding:48px 40px;
          text-align:center;max-width:420px;width:90%;
          box-shadow:0 4px 24px rgba(10,34,64,.10)}
    .logo{font-size:48px;margin-bottom:16px}
    h1{font-size:64px;font-weight:800;color:#007A87;line-height:1}
    h2{font-size:20px;font-weight:600;margin:12px 0 8px}
    p{color:#4B5563;font-size:15px;line-height:1.6;margin-bottom:28px}
    a{display:inline-block;background:#0A2240;color:#fff;
      padding:12px 28px;border-radius:8px;text-decoration:none;
      font-weight:600;font-size:15px;transition:background .2s}
    a:hover{background:#007A87}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">📚</div>
    <h1>404</h1>
    <h2>Page Not Found</h2>
    <p>The page you're looking for doesn't exist or has been moved.</p>
    <a href="/dashboard/login">Go to Dashboard</a>
  </div>
</body>
</html>"""
        return HTMLResponse(html, status_code=404)

    return app


app = create_app()

"""LTI 1.3 OpenID Connect launch flow.

Endpoints
─────────
GET|POST /lti/login      OIDC third-party initiation (Canvas calls this first)
POST     /lti/launch     OIDC callback — validates Canvas JWT, creates session
GET      /lti/jwks       Tool's public JWKS (Canvas fetches to verify our JWTs)
GET      /lti/dev        Dev-mode mock launch (LTI_DEV_MODE=true only)
POST     /lti/register   Register tool against a Canvas instance (admin use)

Canvas configuration checklist (LTI Developer Key):
  Target Link URI   → https://<your-host>/lti/launch
  OpenID Connect Initiation URL → https://<your-host>/lti/login
  JWK Method        → Public JWK URL → https://<your-host>/lti/jwks
  Redirect URIs     → https://<your-host>/lti/launch
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

_log = logging.getLogger(__name__)

import httpx
import jwt
from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .config import settings
from .db import LTISession, LTIState, get_session

router = APIRouter(prefix="/lti", tags=["lti"])

# RSA private key file — generated once on first startup.
# On Render the /data disk is persistent; locally falls back to project root.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
import os as _os
_DATA_DIR = Path(_os.environ.get("DB_PATH", str(PROJECT_ROOT / "sgeg.db"))).parent
_KEY_FILE = _DATA_DIR / "lti_private_key.pem"
_PUB_FILE = _DATA_DIR / "lti_public_key.pem"

# OIDC expiry windows
_STATE_TTL_SECONDS = 600       # 10 minutes for OIDC state
_SESSION_TTL_HOURS = 8         # default: one school day
_SESSION_TTL_FOUNDATION = 2    # Phase 5: Foundation Phase (Grade R–3) — 2 hrs on shared devices

# Canvas LTI 1.3 well-known endpoints (derived from canvas_base_url)
def _canvas_oidc_url() -> str:
    return f"{settings.canvas_base_url}/api/lti/authorize_redirect"

def _canvas_jwks_url() -> str:
    return f"{settings.canvas_base_url}/api/lti/security/jwks"


# ── RSA key management ────────────────────────────────────────────────────────

def _ensure_key_pair() -> tuple[Any, Any]:
    """Return (private_key, public_key), generating and saving them if absent."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    if _KEY_FILE.exists():
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        private_key = load_pem_private_key(_KEY_FILE.read_bytes(), password=None)
        public_key = private_key.public_key()
        return private_key, public_key

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _KEY_FILE.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_key = private_key.public_key()
    _PUB_FILE.write_bytes(
        public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private_key, public_key


def _get_public_jwk() -> dict:
    """Return the tool's public key as a JWK dict."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from jwt.algorithms import RSAAlgorithm

    _, public_key = _ensure_key_pair()
    pem = public_key.public_bytes(encoding=Encoding.PEM, format=PublicFormat.SubjectPublicKeyInfo)
    jwk = json.loads(RSAAlgorithm.to_jwk(public_key))  # type: ignore[attr-defined]
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    jwk["kid"] = "ai-buddy-key-1"
    return jwk


# ── JWT validation ────────────────────────────────────────────────────────────

def _validate_id_token(id_token: str, nonce: str) -> dict:
    """Fetch Canvas JWKS via httpx and validate the LTI id_token.

    Uses httpx instead of PyJWKClient (which uses urllib) so that macOS Python
    certificate verification works correctly — urllib on macOS doesn't pick up
    the system root CAs, but httpx does via certifi.
    """
    from jwt.algorithms import RSAAlgorithm

    try:
        # Fetch JWKS using httpx — SSL works correctly on all platforms
        jwks_resp = httpx.get(
            _canvas_jwks_url(),
            headers={"User-Agent": "AI-Buddy/0.2"},
            timeout=10,
        )
        jwks_resp.raise_for_status()
        jwks = jwks_resp.json()

        # Match the key by kid header
        header = jwt.get_unverified_header(id_token)
        kid = header.get("kid")
        keys = jwks.get("keys", [])
        matched = [k for k in keys if k.get("kid") == kid] or keys
        if not matched:
            raise ValueError("No keys found in Canvas JWKS.")

        public_key = RSAAlgorithm.from_jwk(json.dumps(matched[0]))
        claims = jwt.decode(
            id_token,
            public_key,
            algorithms=["RS256"],
            audience=settings.lti_client_id or None,
            options={"verify_aud": bool(settings.lti_client_id)},
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid id_token: {exc}") from exc

    if claims.get("nonce") != nonce:
        raise HTTPException(status_code=400, detail="Nonce mismatch.")

    msg_type = claims.get(
        "https://purl.imsglobal.org/spec/lti/claim/message_type", ""
    )
    if msg_type not in ("LtiResourceLinkRequest", "LtiDeepLinkingRequest"):
        raise HTTPException(status_code=400, detail=f"Unsupported LTI message type: {msg_type}")

    return claims


# ── Grade-level detection ─────────────────────────────────────────────────────

import re as _re

_GRADE_R_RE = _re.compile(r"\bgrade\s+r\b", _re.IGNORECASE)
_GRADE_N_RE = _re.compile(r"\b(?:grade|gr\.?)\s+(\d{1,2})\b", _re.IGNORECASE)


def _extract_grade_level(course_title: str | None) -> int | None:
    if not course_title:
        return None
    if _GRADE_R_RE.search(course_title):
        return 0
    m = _GRADE_N_RE.search(course_title)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 12 else None
    return None


# ── Enrollment fetch ──────────────────────────────────────────────────────────

def _fetch_enrollment_scope(user_id: str) -> tuple[list[str], list[int], int | None]:
    """Fetch enrolled course IDs, unique sub-account IDs, and launch-account ID.

    Returns (course_ids, account_ids, launch_account_id).
    All empty / None on failure — session proceeds without scoping (dev mode).
    """
    if not re.match(r"^\d+$", user_id or ""):
        return [], [], None

    from .canvas import CanvasClient
    from .config import settings

    if not settings.canvas_api_token or not settings.canvas_base_url:
        return [], [], None

    try:
        with CanvasClient(settings.canvas_base_url, settings.canvas_api_token) as c:
            enrollments = c.get_student_enrollments(int(user_id))  # includes ObserverEnrollment

        course_ids: list[str] = []
        account_ids: set[int] = set()

        with CanvasClient(settings.canvas_base_url, settings.canvas_api_token) as c:
            for enr in enrollments:
                cid = enr.get("course_id")
                if not cid:
                    continue
                course_ids.append(str(cid))
                try:
                    course = c.get_course(int(cid))
                    aid = course.get("account_id")
                    if aid:
                        account_ids.add(int(aid))
                except Exception:
                    pass

        return course_ids, list(account_ids), (list(account_ids)[0] if account_ids else None)

    except Exception as exc:
        _log.warning("_fetch_enrollment_scope failed for user %s: %s", user_id, exc)
        return [], [], None


# ── Session helpers ───────────────────────────────────────────────────────────

def _create_lti_session(claims: dict, platform_id: str) -> str:
    """Persist an LTISession from decoded JWT claims.

    Fetches the student's full enrollment list and sub-account tree at creation
    time so every subsequent tool call can validate scope without a Canvas
    round-trip.
    """
    context    = claims.get("https://purl.imsglobal.org/spec/lti/claim/context", {}) or {}
    custom     = claims.get("https://purl.imsglobal.org/spec/lti/claim/custom", {}) or {}
    roles_list = claims.get("https://purl.imsglobal.org/spec/lti/claim/roles", [])

    # Canvas LTI 1.3 `sub` is an opaque UUID — get the real numeric user ID from
    # the custom claim $Canvas.user.id that we configured in the LTI Developer Key.
    lti_sub       = str(claims.get("sub", ""))
    numeric_uid   = str(custom.get("canvas_user_id", "")).strip()
    canvas_user_id = numeric_uid if re.match(r"^\d+$", numeric_uid) else lti_sub

    # Same for course: context.id is a UUID in Canvas LTI 1.3; numeric ID comes
    # from the custom $Canvas.course.id claim.
    context_uuid   = str(context.get("id", "")) or None
    numeric_cid    = str(custom.get("canvas_course_id", "")).strip()
    course_id      = numeric_cid if re.match(r"^\d+$", numeric_cid) else context_uuid

    course_title = context.get("title") or None
    user_name = (
        claims.get("name")
        or f"{claims.get('given_name', '')} {claims.get('family_name', '')}".strip()
        or "Student"
    )

    _log.info(
        "LTI launch: sub=%s numeric_uid=%s context_uuid=%s numeric_cid=%s",
        lti_sub[:12], canvas_user_id, context_uuid, course_id,
    )

    # Fetch enrollment scope using the numeric user ID — non-blocking
    course_ids, account_ids, launch_acct = _fetch_enrollment_scope(canvas_user_id)

    grade_level = _extract_grade_level(course_title)
    # Phase 5: Foundation Phase (Grade R–3) gets a shorter TTL on shared devices
    ttl_hours = (
        _SESSION_TTL_FOUNDATION if (grade_level is not None and grade_level <= 3)
        else _SESSION_TTL_HOURS
    )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    session_id = str(uuid.uuid4())

    with get_session() as db:
        row = LTISession(
            session_id=session_id,
            user_id=canvas_user_id,
            course_id=course_id,
            user_name=user_name,
            user_email=claims.get("email"),
            roles=",".join(roles_list),
            course_title=course_title,
            grade_level=grade_level,
            platform_id=platform_id,
            enrolled_course_ids=json.dumps(course_ids) if course_ids else None,
            enrolled_account_ids=json.dumps(account_ids) if account_ids else None,
            launch_account_id=launch_acct,
            created_at=now,
            expires_at=now + timedelta(hours=ttl_hours),
        )
        db.add(row)
        db.commit()

    return session_id


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/jwks")
def jwks() -> JSONResponse:
    """Return the tool's public JWKS. Canvas fetches this to verify our JWTs."""
    return JSONResponse({"keys": [_get_public_jwk()]})


@router.get("/login")
@router.post("/login")
async def oidc_login(
    request: Request,
    iss: str = Query(default=""),
    login_hint: str = Query(default=""),
    target_link_uri: str = Query(default=""),
    lti_message_hint: str = Query(default=""),
    client_id: str = Query(default=""),
) -> RedirectResponse:
    """OIDC third-party login initiation.

    Canvas redirects here first. We store state+nonce then send the browser to
    Canvas's OIDC auth endpoint to get the signed id_token.
    """
    # Canvas sometimes sends these as form fields instead of query params.
    if request.method == "POST":
        form = await request.form()
        iss = iss or str(form.get("iss", ""))
        login_hint = login_hint or str(form.get("login_hint", ""))
        target_link_uri = target_link_uri or str(form.get("target_link_uri", ""))
        lti_message_hint = lti_message_hint or str(form.get("lti_message_hint", ""))
        client_id = client_id or str(form.get("client_id", ""))

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)

    with get_session() as db:
        db.add(LTIState(
            state=state,
            nonce=nonce,
            target_link_uri=target_link_uri or "/chat",
        ))
        db.commit()

    base_url = str(request.base_url).rstrip("/")
    redirect_uri = f"{base_url}/lti/launch"

    params = {
        "response_type": "id_token",
        "response_mode": "form_post",
        "scope": "openid",
        "client_id": client_id or settings.lti_client_id,
        "redirect_uri": redirect_uri,
        "login_hint": login_hint,
        "state": state,
        "nonce": nonce,
        "prompt": "none",
    }
    if lti_message_hint:
        params["lti_message_hint"] = lti_message_hint

    return RedirectResponse(f"{_canvas_oidc_url()}?{urlencode(params)}", status_code=302)


@router.post("/launch")
async def oidc_launch(
    request: Request,
    state: str = Form(...),
    id_token: str = Form(...),
) -> RedirectResponse:
    """OIDC callback from Canvas.

    Validates the signed JWT, creates an LTISession, then redirects the
    student's browser to the AI Buddy chat page.
    """
    # Look up and consume the state row.
    with get_session() as db:
        state_row = db.get(LTIState, state)
        if state_row is None:
            raise HTTPException(status_code=400, detail="Unknown or expired OIDC state.")
        nonce = state_row.nonce
        target = state_row.target_link_uri or "/chat"
        db.delete(state_row)
        db.commit()

    platform_id = str(request.base_url).rstrip("/")  # use Canvas iss from claims below
    claims = _validate_id_token(id_token, nonce)
    platform_id = claims.get("iss", platform_id)

    session_id = _create_lti_session(claims, platform_id)
    return RedirectResponse(f"/chat?session={session_id}", status_code=302)


@router.get("/dev", response_class=HTMLResponse)
async def dev_launch(
    request: Request,
    name: str = Query(default="Demo Student"),
    course: str = Query(default="Grade 8 Mathematics"),
    user_id: str = Query(default="demo-user-1"),
    course_id: str = Query(default="demo-course-1"),
) -> RedirectResponse:
    """Dev-mode mock launch — no real Canvas or JWT required.

    Usage: GET /lti/dev?name=Amahle&course=Grade+10+English&user_id=u123

    Only available when LTI_DEV_MODE=true (the default for local dev).
    """
    if not settings.lti_dev_mode:
        raise HTTPException(status_code=403, detail="Dev mode is disabled.")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    session_id = str(uuid.uuid4())

    course_ids, account_ids, launch_acct = _fetch_enrollment_scope(user_id)

    with get_session() as db:
        db.add(LTISession(
            session_id=session_id,
            user_id=user_id,
            course_id=course_id,
            user_name=name,
            user_email=None,
            roles="http://purl.imsglobal.org/vocab/lis/v2/membership#Learner",
            course_title=course,
            grade_level=_extract_grade_level(course),
            platform_id="dev",
            enrolled_course_ids=json.dumps(course_ids) if course_ids else None,
            enrolled_account_ids=json.dumps(account_ids) if account_ids else None,
            launch_account_id=launch_acct,
            created_at=now,
            expires_at=now + timedelta(hours=_SESSION_TTL_HOURS),
        ))
        db.commit()

    return RedirectResponse(f"/chat?session={session_id}", status_code=302)

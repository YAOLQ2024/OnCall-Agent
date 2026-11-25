"""Local password login and HS256 tokens with explicit logout revocation."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.config import config
from app.storage.session_db import session_db_manager

router = APIRouter(prefix="/auth", tags=["auth"])
JWT_SECRET_KEY = config.oncall_jwt_secret.get_secret_value() or session_db_manager.get_or_create_secret("jwt_signing_key")
if len(JWT_SECRET_KEY.encode()) < 32:
    raise ValueError("ONCALL_JWT_SECRET must contain at least 32 bytes")
JWT_EXPIRE_SECONDS = 7 * 86400


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", segment):
        raise ValueError("Invalid base64url")
    data = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    if _b64url_encode(data) != segment:
        raise ValueError("Non-canonical base64url")
    return data


def create_access_token(user_id: str, role: str, expires_in: int = JWT_EXPIRE_SECONDS) -> str:
    now = int(time.time())
    payload = {"sub": user_id, "role": role, "iat": now, "exp": now + expires_in,
               "jti": secrets.token_hex(24), "iss": "oncall-agent", "aud": "oncall-ui"}
    header = _b64url_encode(b'{"alg":"HS256","typ":"JWT"}')
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(JWT_SECRET_KEY.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64url_encode(signature)}"


def _verified_claims(token: str) -> dict | None:
    if not isinstance(token, str) or len(token) > 8192 or token.count(".") != 2:
        return None
    try:
        header, body, signature = token.split(".")
        if json.loads(_b64url_decode(header)) != {"alg": "HS256", "typ": "JWT"}:
            return None
        expected = hmac.new(JWT_SECRET_KEY.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(signature)):
            return None
        claims = json.loads(_b64url_decode(body))
        if not isinstance(claims, dict):
            return None
        now = int(time.time())
        if type(claims.get("exp")) is not int or claims["exp"] <= now:
            return None
        if type(claims.get("iat")) is not int or claims["iat"] > now or claims["iat"] >= claims["exp"]:
            return None
        if claims.get("iss") != "oncall-agent" or claims.get("aud") != "oncall-ui":
            return None
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            return None
        if not isinstance(claims.get("jti"), str) or not re.fullmatch(r"[0-9a-f]{48}", claims["jti"]):
            return None
        if session_db_manager.is_token_revoked(claims["jti"]):
            return None
        return claims
    except (ValueError, TypeError, UnicodeError):
        return None


def verify_access_token(token: str) -> str | None:
    claims = _verified_claims(token)
    return claims["sub"] if claims else None


def _extract_bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="无效的认证请求头", headers={"WWW-Authenticate": "Bearer"})
    return parts[1]


def resolve_user_id(request: Request, authorization: str | None = Header(None)) -> str:
    token = _extract_bearer_token(authorization) if authorization is not None else request.cookies.get("oncall_token")
    uid = verify_access_token(token) if token else None
    if not uid or session_db_manager.get_user(uid) is None:
        raise HTTPException(status_code=401, detail="登录凭证无效或已过期，请重新登录",
                            headers={"WWW-Authenticate": "Bearer"})
    return uid


CurrentUser = Annotated[str, Depends(resolve_user_id)]


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


@router.post("/login")
async def login(req: LoginRequest, response: Response, request: Request) -> dict[str, Any]:
    user = await run_in_threadpool(session_db_manager.authenticate_user, req.username, req.password)
    if user is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误，请核对后重试")
    token = create_access_token(user.user_id, user.role)
    response.set_cookie("oncall_token", token, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", max_age=JWT_EXPIRE_SECONDS)
    response.delete_cookie("oncall_user_id")
    response.headers["Cache-Control"] = "no-store"
    return {"code": 200, "message": "登录成功", "data": {
        "access_token": token, "token_type": "bearer", "current_user_id": user.user_id,
        "user": user.model_dump(),
    }}


@router.get("/me")
@router.get("/current")
async def get_authenticated_me(uid: CurrentUser) -> dict[str, Any]:
    return {"code": 200, "message": "success", "data": session_db_manager.get_user(uid).model_dump()}


@router.post("/logout")
async def logout(request: Request, response: Response, authorization: str | None = Header(None)) -> dict[str, Any]:
    token = _extract_bearer_token(authorization) if authorization is not None else request.cookies.get("oncall_token")
    claims = _verified_claims(token) if token else None
    if claims:
        session_db_manager.revoke_access_token(claims["jti"], claims["exp"])
    response.delete_cookie("oncall_token")
    response.delete_cookie("oncall_user_id")
    response.headers["Cache-Control"] = "no-store"
    return {"code": 200, "message": "已退出登录", "data": {"logged_out": True}}


@router.get("/users")
async def get_all_users(uid: CurrentUser) -> dict[str, Any]:
    return {"code": 200, "message": "success", "data": {
        "current_user_id": uid, "users": [u.model_dump() for u in session_db_manager.list_users()],
    }}


@router.post("/switch")
async def switch_user() -> None:
    raise HTTPException(status_code=410, detail="请使用账号密码登录切换身份")

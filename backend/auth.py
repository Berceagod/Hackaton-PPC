"""JWT auth: UI users (dispecer/admin) and the simulator's service token."""

import hmac
import time
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request

from config import Settings


@dataclass(frozen=True)
class Principal:
    email: str
    role: str  # dispecer | admin | service


def authenticate(settings: Settings, email: str, password: str) -> dict | None:
    user = settings.users.get(email.strip().lower())
    if user is None:
        # still do a comparison to keep timing similar
        hmac.compare_digest(password.encode(), b"x")
        return None
    if not hmac.compare_digest(password.encode(), user["password"].encode()):
        return None
    return {"email": email.strip().lower(), "role": user["role"], "name": user["name"]}


def issue_token(settings: Settings, user: dict) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": user["email"],
            "role": user["role"],
            "name": user["name"],
            "iat": now,
            "exp": now + settings.jwt_ttl_seconds,
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def principal_from_token(settings: Settings, token: str) -> Principal | None:
    if hmac.compare_digest(token.encode(), settings.simulator_token.encode()):
        return Principal(email="simulator@ppc.ro", role="service")
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],  # never "none", never an algorithm chosen by the token
            options={"require": ["exp", "sub", "role"]},
        )
    except jwt.PyJWTError:
        return None
    if not isinstance(claims["sub"], str) or not isinstance(claims["role"], str):
        return None
    return Principal(email=claims["sub"], role=claims["role"])


def current_principal(request: Request) -> Principal:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    principal = principal_from_token(request.app.state.deps.settings, token)
    if principal is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return principal


def require_roles(*roles: str):
    def dep(principal: Principal = Depends(current_principal)) -> Principal:
        if principal.role not in roles:
            raise HTTPException(status_code=403, detail="Insufficient role")
        return principal

    return dep

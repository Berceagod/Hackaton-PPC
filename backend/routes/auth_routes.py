from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from auth import authenticate, issue_token

router = APIRouter()


class LoginBody(BaseModel):
    email: str
    password: str


@router.post("/auth/login")
async def login(body: LoginBody, request: Request) -> dict:
    settings = request.app.state.deps.settings
    user = authenticate(settings, body.email, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"token": issue_token(settings, user), "user": user}

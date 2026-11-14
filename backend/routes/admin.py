"""Policy administration (hot reload, no restart)."""

import logging

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request

from auth import Principal, require_roles
from policies import PolicyError

log = logging.getLogger("filegrid.admin")
router = APIRouter()


@router.get("/admin/policies")
async def get_policies(
    request: Request,
    _: Principal = Depends(require_roles("dispecer", "admin")),
) -> dict:
    return await request.app.state.deps.policies.all()


@router.put("/admin/policies")
async def put_policies(
    request: Request,
    principal: Principal = Depends(require_roles("admin")),
) -> dict:
    """Body: JSON or YAML, either {category: policy} or {categories: {...}}."""
    raw = await request.body()
    try:
        doc = yaml.safe_load(raw)  # JSON is a subset of YAML
        updated = await request.app.state.deps.policies.update(doc)
    except (yaml.YAMLError, PolicyError) as exc:
        raise HTTPException(422, f"Invalid policy: {exc}") from exc
    log.info("policies updated", extra={"user": principal.email, "policies": updated})
    return updated

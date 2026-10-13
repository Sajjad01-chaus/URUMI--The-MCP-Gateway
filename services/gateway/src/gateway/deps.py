from fastapi import Depends, HTTPException, Request

from .auth import Principal, bearer_token
from .models import User
from .sessions import COOKIE_NAME, CSRF_HEADER, read_token

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def services(request: Request):
    return request.app.state.services


async def current_principal(request: Request) -> Principal:
    """API key (Bearer) or dashboard session cookie. The user is re-loaded on every request,
    so disabling a user or revoking a key takes effect immediately."""
    svc = services(request)
    token = bearer_token(request.headers)
    if token:
        principal = await svc.resolve_api_key(token)
        if principal is None:
            raise HTTPException(401, "invalid or revoked API key")
        return principal

    session_token = request.cookies.get(COOKIE_NAME)
    user_id = read_token(session_token, svc.settings.session_secret, "session") if session_token else None
    if user_id is None:
        raise HTTPException(401, "authentication required")
    async with svc.db.session() as session:
        user = await session.get(User, user_id)
    if user is None or user.status != "active":
        raise HTTPException(401, "session is no longer valid")
    # CSRF: browsers can't add custom headers to cross-site form posts, so cookie-authenticated
    # writes must carry X-Requested-With (the dashboard always sends it).
    if request.method not in SAFE_METHODS and not request.headers.get(CSRF_HEADER):
        raise HTTPException(403, "missing X-Requested-With header")
    return Principal(user.id, user.email, user.role, None, "dashboard")


async def require_admin(principal: Principal = Depends(current_principal)) -> Principal:
    if principal.role != "admin":
        raise HTTPException(403, "admin role required")
    return principal

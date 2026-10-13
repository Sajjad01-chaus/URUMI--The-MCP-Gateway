import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from .auth import Principal
from .deps import current_principal, require_admin, services
from .models import ApiKey, Invite, User, utcnow
from .security import generate_api_key, hash_password, hash_token, verify_password
from .sessions import COOKIE_NAME, issue_token

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")
Role = Literal["member", "admin"]
MIN_PASSWORD = 10
# Verified against when the email is unknown, so a wrong email and a wrong password take the same time.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    role: str
    status: str
    created_at: datetime
    last_login_at: datetime | None
    active_keys: int = 0


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)


class InviteIn(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    role: Role = "member"


class InviteOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    expires_at: datetime
    link: str | None = None


class AcceptIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=MIN_PASSWORD, max_length=200)


class MemberPatch(BaseModel):
    role: Role | None = None
    status: Literal["active", "disabled"] | None = None


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class KeyOut(BaseModel):
    id: uuid.UUID
    user_email: str
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class KeyCreated(KeyOut):
    key: str


def user_out(user: User, active_keys: int = 0) -> UserOut:
    return UserOut(
        id=user.id, email=user.email, name=user.name, role=user.role, status=user.status,
        created_at=user.created_at, last_login_at=user.last_login_at, active_keys=active_keys,
    )


def key_out(key: ApiKey, email: str) -> KeyOut:
    return KeyOut(
        id=key.id, user_email=email, name=key.name, prefix=key.prefix, created_at=key.created_at,
        last_used_at=key.last_used_at, revoked_at=key.revoked_at,
    )


def start_session(response: Response, request: Request, user_id: uuid.UUID) -> None:
    settings = services(request).settings
    response.set_cookie(
        COOKIE_NAME, issue_token(user_id, settings.session_secret, settings.session_ttl_seconds, "session"),
        max_age=settings.session_ttl_seconds, httponly=True, samesite="lax", secure=settings.cookie_secure, path="/",
    )


def normalise(email: str) -> str:
    return email.strip().lower()


# --- session ---

@router.post("/auth/login", response_model=UserOut)
async def login(data: LoginIn, request: Request, response: Response):
    svc = services(request)
    email = normalise(data.email)
    if svc.throttle.blocked(email):
        log.warning("login throttled for %s", email)
        raise HTTPException(429, "too many failed logins; try again in a few minutes")
    async with svc.db.session() as session:
        user = await session.scalar(select(User).where(User.email == email))
        ok = verify_password(user.password_hash if user else DUMMY_HASH, data.password)
        if not ok or user is None or user.status != "active":
            svc.throttle.fail(email)
            log.warning("failed login for %s", email)
            raise HTTPException(401, "wrong email or password")
        user.last_login_at = utcnow()
        await session.commit()
    svc.throttle.reset(email)
    start_session(response, request, user.id)
    log.info("login %s", email)
    return user_out(user)


@router.post("/auth/logout", status_code=204)
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")


@router.get("/auth/me", response_model=UserOut)
async def me(request: Request, principal: Principal = Depends(current_principal)):
    async with services(request).db.session() as session:
        user = await session.get(User, principal.user_id)
    if user is None:
        raise HTTPException(401, "session is no longer valid")
    return user_out(user)


# --- invites ---

@router.post("/invites", response_model=InviteOut, status_code=201)
async def create_invite(data: InviteIn, request: Request, admin: Principal = Depends(require_admin)):
    """Returns a one-time link (no email server); only the token's hash is stored."""
    svc = services(request)
    email = normalise(data.email)
    token = secrets.token_urlsafe(32)
    async with svc.db.session() as session:
        if await session.scalar(select(User.id).where(User.email == email)):
            raise HTTPException(409, f"{email} is already a member")
        # A new invite replaces any older pending one for the same email.
        for old in (await session.scalars(select(Invite).where(Invite.email == email, Invite.accepted_at.is_(None)))).all():
            await session.delete(old)
        invite = Invite(
            email=email, role=data.role, token_hash=hash_token(token), invited_by=admin.user_id,
            expires_at=utcnow() + timedelta(hours=svc.settings.invite_ttl_hours),
        )
        session.add(invite)
        await session.commit()
    link = f"{svc.settings.public_app_url.rstrip('/')}/invite/{token}"
    log.info("%s invited %s as %s", admin.email, email, invite.role)
    return InviteOut(id=invite.id, email=email, role=invite.role, expires_at=invite.expires_at, link=link)


@router.get("/invites", response_model=list[InviteOut])
async def list_invites(request: Request, _: Principal = Depends(require_admin)):
    async with services(request).db.session() as session:
        rows = (await session.scalars(
            select(Invite).where(Invite.accepted_at.is_(None)).order_by(Invite.created_at.desc())
        )).all()
    now = utcnow()
    return [InviteOut(id=i.id, email=i.email, role=i.role, expires_at=i.expires_at) for i in rows if aware(i.expires_at) > now]


@router.delete("/invites/{invite_id}", status_code=204)
async def cancel_invite(invite_id: uuid.UUID, request: Request, admin: Principal = Depends(require_admin)):
    async with services(request).db.session() as session:
        invite = await session.get(Invite, invite_id)
        if invite is None or invite.accepted_at is not None:
            raise HTTPException(404, "invite not found")
        await session.delete(invite)
        await session.commit()
    log.info("%s cancelled the invite for %s", admin.email, invite.email)


async def pending_invite(session, token: str) -> Invite:
    invite = await session.scalar(select(Invite).where(Invite.token_hash == hash_token(token)))
    if invite is None or invite.accepted_at is not None or aware(invite.expires_at) <= utcnow():
        raise HTTPException(404, "this invite link is invalid, expired or already used")
    return invite


@router.get("/invites/{token}", response_model=InviteOut)
async def view_invite(token: str, request: Request):
    async with services(request).db.session() as session:
        invite = await pending_invite(session, token)
    return InviteOut(id=invite.id, email=invite.email, role=invite.role, expires_at=invite.expires_at)


@router.post("/invites/{token}/accept", response_model=UserOut, status_code=201)
async def accept_invite(token: str, data: AcceptIn, request: Request, response: Response):
    async with services(request).db.session() as session:
        invite = await pending_invite(session, token)
        if await session.scalar(select(User.id).where(User.email == invite.email)):
            raise HTTPException(409, f"{invite.email} is already a member")
        user = User(email=invite.email, name=data.name.strip(), role=invite.role,
                    password_hash=hash_password(data.password), last_login_at=utcnow())
        session.add(user)
        invite.accepted_at = utcnow()
        email = invite.email
        try:
            await session.commit()
        except IntegrityError as exc:
            # Two accepts racing for the same email: the unique constraint lets only one win.
            await session.rollback()
            log.info("rejected a concurrent invite accept for %s", email)
            raise HTTPException(409, f"{email} is already a member") from exc
    start_session(response, request, user.id)
    log.info("%s accepted an invite as %s", user.email, user.role)
    return user_out(user)


# --- members ---

@router.get("/members", response_model=list[UserOut])
async def list_members(request: Request, _: Principal = Depends(require_admin)):
    async with services(request).db.session() as session:
        counts = dict((await session.execute(
            select(ApiKey.user_id, func.count()).where(ApiKey.revoked_at.is_(None)).group_by(ApiKey.user_id)
        )).all())
        users = (await session.scalars(select(User).order_by(User.created_at))).all()
    return [user_out(u, counts.get(u.id, 0)) for u in users]


@router.patch("/members/{user_id}", response_model=UserOut)
async def update_member(user_id: uuid.UUID, patch: MemberPatch, request: Request, admin: Principal = Depends(require_admin)):
    """Disabling a user blocks their dashboard session and all their API keys on the next request."""
    async with services(request).db.session() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise HTTPException(404, "member not found")
        new_role = patch.role or user.role
        new_status = patch.status or user.status
        if user.role == "admin" and user.status == "active" and (new_role != "admin" or new_status != "active"):
            admins = await session.scalar(
                select(func.count()).select_from(User).where(User.role == "admin", User.status == "active")
            )
            if admins <= 1:
                raise HTTPException(409, "the team must keep at least one active admin")
        user.role, user.status = new_role, new_status
        await session.commit()
    log.info("%s set %s to role=%s status=%s", admin.email, user.email, new_role, new_status)
    return user_out(user)


# --- API keys ---

@router.get("/keys", response_model=list[KeyOut])
async def list_keys(request: Request, principal: Principal = Depends(current_principal)):
    """Members see their own keys; admins see everyone's."""
    query = select(ApiKey, User.email).join(User, ApiKey.user_id == User.id).order_by(ApiKey.created_at.desc())
    if principal.role != "admin":
        query = query.where(ApiKey.user_id == principal.user_id)
    async with services(request).db.session() as session:
        rows = (await session.execute(query)).all()
    return [key_out(k, email) for k, email in rows]


@router.post("/keys", response_model=KeyCreated, status_code=201)
async def create_key(data: KeyIn, request: Request, principal: Principal = Depends(current_principal)):
    """The plaintext key is returned once and never stored. Needs a dashboard session,
    so a leaked API key can't mint a replacement for itself."""
    if principal.source != "dashboard":
        raise HTTPException(403, "API keys can only be created from a dashboard session")
    key, prefix, key_hash = generate_api_key()
    async with services(request).db.session() as session:
        row = ApiKey(user_id=principal.user_id, name=data.name.strip(), prefix=prefix, key_hash=key_hash)
        session.add(row)
        await session.commit()
    log.info("%s created API key %s (%s)", principal.email, prefix, row.name)
    return KeyCreated(**key_out(row, principal.email).model_dump(), key=key)


@router.delete("/keys/{key_id}", status_code=204)
async def revoke_key(key_id: uuid.UUID, request: Request, principal: Principal = Depends(current_principal)):
    """Takes effect on the key's next request: every /mcp request re-checks the key in the database."""
    async with services(request).db.session() as session:
        key = await session.get(ApiKey, key_id)
        if key is None or (key.user_id != principal.user_id and principal.role != "admin"):
            raise HTTPException(404, "key not found")
        if key.revoked_at is None:
            key.revoked_at, key.revoked_by = utcnow(), principal.user_id
            await session.commit()
            log.info("%s revoked API key %s", principal.email, key.prefix)

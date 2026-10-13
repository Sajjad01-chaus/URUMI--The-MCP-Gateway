import time
import uuid

import jwt

COOKIE_NAME = "urumi_session"
CSRF_HEADER = "x-requested-with"


def issue_token(user_id: uuid.UUID, secret: str, ttl_seconds: int, kind: str) -> str:
    """Short signed token (HS256). `kind` separates dashboard sessions from chat tokens."""
    now = int(time.time())
    return jwt.encode({"sub": str(user_id), "typ": kind, "iat": now, "exp": now + ttl_seconds}, secret, algorithm="HS256")


def read_token(token: str, secret: str, kind: str) -> uuid.UUID | None:
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp", "sub", "typ"]})
    except jwt.PyJWTError:
        return None
    if claims.get("typ") != kind:
        return None
    try:
        return uuid.UUID(claims["sub"])
    except ValueError:
        return None


class LoginThrottle:
    """At most `limit` failed logins per email per `window` seconds (in-memory; single replica in v1)."""

    def __init__(self, limit: int = 5, window: int = 300, max_tracked: int = 10_000):
        self.limit, self.window, self.max_tracked = limit, window, max_tracked
        self._failures: dict[str, list[float]] = {}

    def blocked(self, email: str) -> bool:
        now = time.monotonic()
        recent = [t for t in self._failures.get(email, []) if now - t < self.window]
        # Drop expired entries so random emails can't grow this dict forever.
        if recent:
            self._failures[email] = recent
        else:
            self._failures.pop(email, None)
        return len(recent) >= self.limit

    def fail(self, email: str) -> None:
        now = time.monotonic()
        if len(self._failures) >= self.max_tracked:
            self._failures = {e: ts for e, ts in self._failures.items() if now - ts[-1] < self.window}
        self._failures.setdefault(email, []).append(now)

    def reset(self, email: str) -> None:
        self._failures.pop(email, None)

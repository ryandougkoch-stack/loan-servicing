"""
app/core/token_denylist.py

Redis-backed token denylist for JWT revocation.

Problem: JWTs are stateless — once issued they are valid until expiry.
For a financial system with 60-minute tokens, a logged-out token stays
live for up to an hour. That's unacceptable if:
  - A user is deactivated mid-session
  - A session is compromised and the user logs out from another device
  - An admin revokes a user's access

Solution: Store revoked token JTIs (JWT IDs) in Redis with a TTL equal
to the token's remaining lifetime. The dependency layer checks the denylist
on every request. When the token would have expired naturally, the Redis
key also expires — no unbounded list growth.

JTI (JWT ID) is a unique identifier we embed in every token we issue.
It's a UUID4 — 36 chars, low collision risk, not guessable.

Redis key pattern:
  token_denylist:{jti}  →  "1"  (TTL = remaining token lifetime in seconds)
  user_sessions:{user_id}  →  Set of active JTIs (for logout-all-devices)
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)


class TokenDenylist:
    """
    Manages token revocation via Redis.
    Instantiated once as a module-level singleton.
    """

    def __init__(self):
        self._redis = None

    def _get_redis(self):
        """Lazy-load the Redis client to avoid import-time connection."""
        if self._redis is None:
            import redis.asyncio as aioredis
            from app.core.config import settings
            self._redis = aioredis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
            )
        return self._redis

    async def revoke_token(self, jti: str, expires_at: datetime) -> None:
        """
        Add a JTI to the denylist.
        TTL is set to the token's remaining lifetime so Redis auto-cleans it.
        """
        now = datetime.now(timezone.utc)
        remaining_seconds = max(0, int((expires_at - now).total_seconds()))
        if remaining_seconds <= 0:
            return   # Already expired — no need to store

        redis = self._get_redis()
        try:
            await redis.setex(
                name=f"token_denylist:{jti}",
                time=remaining_seconds,
                value="1",
            )
            logger.info("token_revoked", jti=jti, ttl_seconds=remaining_seconds)
        except Exception as e:
            # Log but don't fail the logout request — worst case the token
            # expires naturally within ACCESS_TOKEN_EXPIRE_MINUTES
            logger.error("token_revocation_failed", jti=jti, error=str(e))

    async def is_revoked(self, jti: str) -> bool:
        """
        Check if a JTI is in the denylist.
        Returns False on Redis errors (fail-open) to avoid locking out users
        during Redis downtime. Log the error for monitoring.
        """
        redis = self._get_redis()
        try:
            result = await redis.get(f"token_denylist:{jti}")
            return result is not None
        except Exception as e:
            logger.error("denylist_check_failed", jti=jti, error=str(e))
            return False   # fail-open: don't block requests if Redis is down

    async def revoke_all_user_tokens(self, user_id: str) -> int:
        """
        Revoke all active tokens for a user (logout-all-devices).
        Reads the user's active JTI set from Redis and revokes each one.
        Returns the number of tokens revoked.
        """
        redis = self._get_redis()
        try:
            session_key = f"user_sessions:{user_id}"
            jti_expiry_pairs = await redis.hgetall(session_key)
            count = 0
            for jti, expires_str in jti_expiry_pairs.items():
                try:
                    expires_at = datetime.fromisoformat(expires_str)
                    await self.revoke_token(jti, expires_at)
                    count += 1
                except Exception:
                    pass
            # Clear the session map
            await redis.delete(session_key)
            logger.info("all_user_tokens_revoked", user_id=user_id, count=count)
            return count
        except Exception as e:
            logger.error("revoke_all_failed", user_id=user_id, error=str(e))
            return 0

    async def track_session(self, user_id: str, jti: str, expires_at: datetime) -> None:
        """
        Track an active session so logout-all-devices can find it.
        Stores {jti: expires_at_iso} in a Redis hash keyed by user_id.
        """
        redis = self._get_redis()
        try:
            session_key = f"user_sessions:{user_id}"
            await redis.hset(session_key, jti, expires_at.isoformat())
            # Expire the session map 30 days after last activity
            await redis.expire(session_key, 30 * 24 * 3600)
        except Exception as e:
            logger.warning("session_tracking_failed", user_id=user_id, error=str(e))


# Module-level singleton
token_denylist = TokenDenylist()

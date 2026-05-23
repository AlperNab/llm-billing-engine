"""Core BillingEngine — the main entry point for all billing operations."""
from __future__ import annotations
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
import asyncpg

from .models import Balance, LedgerEntry, LedgerEntryType, Reservation
from .exceptions import (
    DuplicateOperationError,
    InsufficientCreditsError,
    ReservationExpiredError,
    ReservationNotFoundError,
)


class BillingEngine:
    """
    Drop-in token credit system for AI SaaS.

    Uses reserve-then-settle pattern with row-level locking to prevent
    double-spend under concurrent load. All amounts stored as DECIMAL(20,8).
    Balance is always computed from the immutable ledger — never stored.
    """

    def __init__(
        self,
        database_url: str,
        reservation_ttl_seconds: int = 300,
        credit_precision: int = 8,
        enable_audit_log: bool = True,
    ):
        self._database_url = database_url
        self._reservation_ttl = reservation_ttl_seconds
        self._precision = credit_precision
        self._audit = enable_audit_log
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        """Create connection pool. Call once at app startup."""
        self._pool = await asyncpg.create_pool(
            self._database_url,
            min_size=2,
            max_size=20,
            command_timeout=30,
        )
        await self._setup_schema()

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()

    # ─── Public API ────────────────────────────────────────────────────────────

    async def add_credits(
        self,
        user_id: str,
        amount: Decimal | float | int,
        reason: str = "manual",
        idempotency_key: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> LedgerEntry:
        """Add credits to a user's balance. Idempotent if key provided."""
        amount = Decimal(str(amount))
        if amount <= 0:
            raise ValueError("amount must be positive")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if idempotency_key:
                    existing = await self._check_idempotency(conn, idempotency_key)
                    if existing:
                        raise DuplicateOperationError(idempotency_key)

                balance = await self._compute_balance_locked(conn, user_id)
                new_balance = balance.available + balance.reserved + amount
                entry_id = str(uuid.uuid4())

                await conn.execute("""
                    INSERT INTO llm_credits_ledger
                        (id, user_id, entry_type, amount, balance_after,
                         idempotency_key, metadata, created_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                """, entry_id, user_id, LedgerEntryType.CREDIT_ADD.value,
                    amount, new_balance, idempotency_key,
                    str(metadata or {"reason": reason}),
                    datetime.now(timezone.utc))

                if idempotency_key:
                    await self._record_idempotency(conn, idempotency_key, entry_id)

                return LedgerEntry(
                    id=entry_id, user_id=user_id,
                    entry_type=LedgerEntryType.CREDIT_ADD,
                    amount=amount, balance_after=new_balance,
                    reservation_id=None, idempotency_key=idempotency_key,
                    metadata=metadata or {"reason": reason},
                    created_at=datetime.now(timezone.utc),
                )

    async def reserve(
        self,
        user_id: str,
        estimated_tokens: int,
        cost_per_token: Optional[Decimal] = None,
        estimated_cost: Optional[Decimal] = None,
        idempotency_key: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Reservation:
        """
        Reserve credits before an AI call. Prevents overspend under concurrency.
        Provide either cost_per_token or estimated_cost directly.
        """
        if estimated_cost is None and cost_per_token is None:
            raise ValueError("Provide either estimated_cost or cost_per_token")

        cost = estimated_cost or Decimal(str(cost_per_token)) * estimated_tokens
        cost = Decimal(str(cost))
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=self._reservation_ttl)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if idempotency_key:
                    existing = await self._check_idempotency(conn, idempotency_key)
                    if existing:
                        raise DuplicateOperationError(idempotency_key)

                # Lock the user's balance row — prevents concurrent reservations
                balance = await self._compute_balance_locked(conn, user_id)

                if balance.available < cost:
                    raise InsufficientCreditsError(user_id, cost, balance.available)

                reservation_id = str(uuid.uuid4())
                now = datetime.now(timezone.utc)

                await conn.execute("""
                    INSERT INTO llm_reservations
                        (id, user_id, estimated_tokens, estimated_cost,
                         status, idempotency_key, expires_at, created_at)
                    VALUES ($1,$2,$3,$4,'pending',$5,$6,$7)
                """, reservation_id, user_id, estimated_tokens, cost,
                    idempotency_key, expires_at, now)

                new_balance = balance.available - cost
                entry_id = str(uuid.uuid4())
                await conn.execute("""
                    INSERT INTO llm_credits_ledger
                        (id, user_id, entry_type, amount, balance_after,
                         reservation_id, idempotency_key, metadata, created_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                """, entry_id, user_id, LedgerEntryType.RESERVATION.value,
                    -cost, new_balance, reservation_id, idempotency_key,
                    str(metadata or {}), now)

                if idempotency_key:
                    await self._record_idempotency(conn, idempotency_key, reservation_id)

                return Reservation(
                    id=reservation_id, user_id=user_id,
                    estimated_tokens=estimated_tokens, estimated_cost=cost,
                    actual_tokens=None, actual_cost=None,
                    status="pending", idempotency_key=idempotency_key,
                    expires_at=expires_at, created_at=now, settled_at=None,
                )

    async def settle(
        self,
        reservation_id: str,
        actual_tokens: int,
        cost_per_token: Optional[Decimal] = None,
        actual_cost: Optional[Decimal] = None,
    ) -> Reservation:
        """
        Settle a reservation with actual token usage.
        Returns any unused reserved credits to the user automatically.
        """
        if actual_cost is None and cost_per_token is None:
            raise ValueError("Provide either actual_cost or cost_per_token")

        cost = actual_cost or Decimal(str(cost_per_token)) * actual_tokens
        cost = Decimal(str(cost))

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                res = await conn.fetchrow("""
                    SELECT * FROM llm_reservations
                    WHERE id = $1 AND status = 'pending'
                    FOR UPDATE
                """, reservation_id)

                if not res:
                    raise ReservationNotFoundError(reservation_id)

                if res["expires_at"] < datetime.now(timezone.utc):
                    raise ReservationExpiredError(reservation_id)

                now = datetime.now(timezone.utc)
                estimated_cost = Decimal(str(res["estimated_cost"]))
                returned = estimated_cost - cost   # May be negative if actual > estimated

                await conn.execute("""
                    UPDATE llm_reservations
                    SET status='settled', actual_tokens=$2, actual_cost=$3, settled_at=$4
                    WHERE id = $1
                """, reservation_id, actual_tokens, cost, now)

                balance = await self._compute_balance_locked(conn, res["user_id"])
                new_balance = balance.available + balance.reserved + returned

                await conn.execute("""
                    INSERT INTO llm_credits_ledger
                        (id, user_id, entry_type, amount, balance_after,
                         reservation_id, metadata, created_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                """, str(uuid.uuid4()), res["user_id"],
                    LedgerEntryType.SETTLEMENT.value,
                    returned,            # positive = returned to user, negative = extra charge
                    new_balance, reservation_id,
                    f'{{"actual_tokens": {actual_tokens}}}', now)

                return Reservation(
                    id=reservation_id, user_id=res["user_id"],
                    estimated_tokens=res["estimated_tokens"],
                    estimated_cost=estimated_cost,
                    actual_tokens=actual_tokens, actual_cost=cost,
                    status="settled", idempotency_key=res["idempotency_key"],
                    expires_at=res["expires_at"], created_at=res["created_at"],
                    settled_at=now,
                )

    async def release(self, reservation_id: str) -> None:
        """Release a reservation — fully returns reserved credits to user."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                res = await conn.fetchrow("""
                    SELECT * FROM llm_reservations
                    WHERE id = $1 AND status = 'pending'
                    FOR UPDATE
                """, reservation_id)

                if not res:
                    raise ReservationNotFoundError(reservation_id)

                now = datetime.now(timezone.utc)
                estimated_cost = Decimal(str(res["estimated_cost"]))

                await conn.execute("""
                    UPDATE llm_reservations SET status='released' WHERE id=$1
                """, reservation_id)

                balance = await self._compute_balance_locked(conn, res["user_id"])
                new_balance = balance.available + balance.reserved + estimated_cost

                await conn.execute("""
                    INSERT INTO llm_credits_ledger
                        (id, user_id, entry_type, amount, balance_after,
                         reservation_id, metadata, created_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                """, str(uuid.uuid4()), res["user_id"],
                    LedgerEntryType.RELEASE.value,
                    estimated_cost, new_balance, reservation_id, '{}', now)

    async def get_balance(self, user_id: str) -> Balance:
        """Get current balance. Always computed from ledger — never stale."""
        async with self._pool.acquire() as conn:
            return await self._compute_balance(conn, user_id)

    # ─── Internal ──────────────────────────────────────────────────────────────

    async def _compute_balance(self, conn, user_id: str) -> Balance:
        row = await conn.fetchrow("""
            SELECT
                COALESCE(SUM(CASE WHEN entry_type='credit_add' THEN amount ELSE 0 END), 0) AS total_added,
                COALESCE(SUM(CASE WHEN entry_type='settlement' THEN -amount ELSE 0 END), 0) AS total_spent,
                COALESCE(SUM(CASE WHEN entry_type='reservation' THEN -amount ELSE 0 END), 0) AS total_reserved_gross,
                COALESCE(SUM(CASE WHEN entry_type IN ('release','settlement') AND amount > 0 THEN amount ELSE 0 END), 0) AS total_returned
            FROM llm_credits_ledger
            WHERE user_id = $1
        """, user_id)

        reserved_row = await conn.fetchrow("""
            SELECT COALESCE(SUM(estimated_cost), 0) AS reserved
            FROM llm_reservations
            WHERE user_id=$1 AND status='pending' AND expires_at > NOW()
        """, user_id)

        total_added = Decimal(str(row["total_added"]))
        total_spent = Decimal(str(row["total_spent"]))
        reserved = Decimal(str(reserved_row["reserved"]))
        available = total_added - total_spent - reserved

        return Balance(
            user_id=user_id,
            available=max(Decimal("0"), available),
            reserved=reserved,
            total_added=total_added,
            total_spent=total_spent,
            computed_at=datetime.now(timezone.utc),
        )

    async def _compute_balance_locked(self, conn, user_id: str) -> Balance:
        """Balance with advisory lock — prevents concurrent reservation races."""
        # Advisory lock keyed on user_id hash
        lock_key = abs(hash(user_id)) % (2**31)
        await conn.execute("SELECT pg_advisory_xact_lock($1)", lock_key)
        return await self._compute_balance(conn, user_id)

    async def _check_idempotency(self, conn, key: str) -> bool:
        row = await conn.fetchrow(
            "SELECT id FROM llm_idempotency_keys WHERE key=$1", key
        )
        return row is not None

    async def _record_idempotency(self, conn, key: str, ref_id: str) -> None:
        await conn.execute("""
            INSERT INTO llm_idempotency_keys (key, reference_id, created_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (key) DO NOTHING
        """, key, ref_id)

    async def _setup_schema(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS llm_credits_ledger (
                    id               TEXT PRIMARY KEY,
                    user_id          TEXT NOT NULL,
                    entry_type       TEXT NOT NULL,
                    amount           DECIMAL(20,8) NOT NULL,
                    balance_after    DECIMAL(20,8) NOT NULL,
                    reservation_id   TEXT,
                    idempotency_key  TEXT,
                    metadata         TEXT DEFAULT '{}',
                    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_ledger_user_id ON llm_credits_ledger(user_id);
                CREATE INDEX IF NOT EXISTS idx_ledger_created ON llm_credits_ledger(created_at);

                CREATE TABLE IF NOT EXISTS llm_reservations (
                    id               TEXT PRIMARY KEY,
                    user_id          TEXT NOT NULL,
                    estimated_tokens INTEGER NOT NULL,
                    estimated_cost   DECIMAL(20,8) NOT NULL,
                    actual_tokens    INTEGER,
                    actual_cost      DECIMAL(20,8),
                    status           TEXT NOT NULL DEFAULT 'pending',
                    idempotency_key  TEXT,
                    expires_at       TIMESTAMPTZ NOT NULL,
                    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    settled_at       TIMESTAMPTZ
                );
                CREATE INDEX IF NOT EXISTS idx_res_user_status ON llm_reservations(user_id, status);
                CREATE INDEX IF NOT EXISTS idx_res_expires ON llm_reservations(expires_at);

                CREATE TABLE IF NOT EXISTS llm_idempotency_keys (
                    key              TEXT PRIMARY KEY,
                    reference_id     TEXT NOT NULL,
                    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)

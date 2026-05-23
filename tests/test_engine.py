"""Tests for BillingEngine — run against a real PostgreSQL instance."""
import pytest
import asyncio
from decimal import Decimal
from llm_billing import BillingEngine, InsufficientCreditsError, DuplicateOperationError

DB_URL = "postgresql://postgres:postgres@localhost/llm_billing_test"


@pytest.fixture
async def engine():
    async with BillingEngine(DB_URL) as e:
        yield e


@pytest.mark.asyncio
async def test_add_and_balance(engine):
    uid = "test_user_1"
    await engine.add_credits(uid, 100)
    balance = await engine.get_balance(uid)
    assert balance.available == Decimal("100")


@pytest.mark.asyncio
async def test_reserve_and_settle(engine):
    uid = "test_user_2"
    await engine.add_credits(uid, 100)
    reservation = await engine.reserve(uid, estimated_tokens=1000, estimated_cost=Decimal("10"))
    assert reservation.is_pending

    balance = await engine.get_balance(uid)
    assert balance.available == Decimal("90")
    assert balance.reserved == Decimal("10")

    await engine.settle(reservation.id, actual_tokens=800, actual_cost=Decimal("8"))
    balance = await engine.get_balance(uid)
    # 90 available + 2 returned from reservation
    assert balance.available == Decimal("92")
    assert balance.reserved == Decimal("0")


@pytest.mark.asyncio
async def test_insufficient_credits(engine):
    uid = "test_user_3"
    await engine.add_credits(uid, 5)
    with pytest.raises(InsufficientCreditsError):
        await engine.reserve(uid, estimated_tokens=1000, estimated_cost=Decimal("10"))


@pytest.mark.asyncio
async def test_release_restores_credits(engine):
    uid = "test_user_4"
    await engine.add_credits(uid, 50)
    res = await engine.reserve(uid, estimated_tokens=500, estimated_cost=Decimal("20"))
    await engine.release(res.id)
    balance = await engine.get_balance(uid)
    assert balance.available == Decimal("50")


@pytest.mark.asyncio
async def test_idempotency(engine):
    uid = "test_user_5"
    await engine.add_credits(uid, 100, idempotency_key="add_001")
    with pytest.raises(DuplicateOperationError):
        await engine.add_credits(uid, 100, idempotency_key="add_001")
    balance = await engine.get_balance(uid)
    assert balance.available == Decimal("100")  # Only added once


@pytest.mark.asyncio
async def test_concurrent_reservations_no_overspend(engine):
    """Two concurrent reservations should not both succeed if only enough for one."""
    uid = "test_user_6"
    await engine.add_credits(uid, 10)
    results = await asyncio.gather(
        engine.reserve(uid, estimated_tokens=500, estimated_cost=Decimal("8")),
        engine.reserve(uid, estimated_tokens=500, estimated_cost=Decimal("8")),
        return_exceptions=True
    )
    successes = [r for r in results if isinstance(r, object) and not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], InsufficientCreditsError)

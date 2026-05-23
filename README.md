# llm-billing-engine

> **Drop-in token credit system for AI SaaS** — reserve, settle, audit. Battle-tested in production at [ToScribe](https://toscribe.to).

[![PyPI](https://img.shields.io/pypi/v/llm-billing-engine?style=flat)](https://pypi.org/project/llm-billing-engine/)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat&logo=python)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-12%2B-316192?style=flat&logo=postgresql)](https://postgresql.org)

Every AI SaaS founder builds the same billing system from scratch. This is the production-grade version — with the hard parts done right.

## The hard parts it solves

| Problem | Naive approach | This library |
|---------|---------------|--------------|
| Floating point money | `FLOAT` columns | `DECIMAL(20,8)` everywhere |
| Double-spend under load | No locking | Row-level `SELECT FOR UPDATE` |
| Charge before confirming usage | Post-deduct | Reserve → use → settle pattern |
| No audit trail | Mutable balance | Immutable ledger, balance is computed |
| Concurrent requests | Race conditions | Idempotency keys |

## Quickstart

```bash
pip install llm-billing-engine
```

```python
from llm_billing import BillingEngine, CreditLedger

engine = BillingEngine(database_url="postgresql://...")

# Add credits to a user
await engine.add_credits(user_id="usr_123", amount=1000, reason="subscription_renewal")

# Before an AI call — reserve credits (prevents overspend)
reservation = await engine.reserve(
    user_id="usr_123",
    estimated_tokens=5000,
    cost_per_token=0.000003,
    idempotency_key="req_abc123"
)

# After the AI call completes — settle with actual usage
await engine.settle(
    reservation_id=reservation.id,
    actual_tokens=4823,
)

# If the call failed — release the reservation
await engine.release(reservation_id=reservation.id)

# Check balance (always computed from ledger, never stored)
balance = await engine.get_balance(user_id="usr_123")
print(f"Credits remaining: {balance.available}")
```

## How it works

```
User balance = SUM(credits added) - SUM(credits settled) - SUM(credits reserved pending)
```

The balance is never stored — it's always computed from the immutable ledger. This means you can always audit exactly what happened and replay history.

```
add_credits(1000)     → ledger: +1000  | balance: 1000
reserve(est=500)      → ledger: -500r  | balance: 500  (500 reserved)
settle(actual=423)    → ledger: -423   | balance: 577  (77 returned from reserve)
```

## Database schema

```sql
-- Run once to set up
SELECT llm_billing_setup();
```

Creates:
- `llm_credits_ledger` — immutable append-only ledger (DECIMAL(20,8))
- `llm_reservations` — in-flight reservations with expiry
- `llm_idempotency_keys` — prevents duplicate operations

## Configuration

```python
engine = BillingEngine(
    database_url="postgresql://user:pass@host/db",
    reservation_ttl_seconds=300,    # Expire stuck reservations after 5 min
    credit_precision=8,              # Decimal places (default: 8)
    enable_audit_log=True,           # Log all operations (default: True)
)
```

## Supported model pricing presets

```python
from llm_billing.presets import ModelPricing

pricing = ModelPricing.GEMINI_25_FLASH   # $0.075 / 1M input tokens
pricing = ModelPricing.CLAUDE_SONNET_4   # $3.00 / 1M input tokens
pricing = ModelPricing.GPT4O             # $2.50 / 1M input tokens

reservation = await engine.reserve(
    user_id="usr_123",
    estimated_tokens=5000,
    pricing=ModelPricing.GEMINI_25_FLASH,
)
```

## FastAPI integration

```python
from llm_billing.integrations.fastapi import BillingMiddleware

app.add_middleware(BillingMiddleware, engine=engine, header="X-User-Id")
```

Automatically checks balance before routes tagged with `@requires_credits(estimated=1000)`.

## License

MIT © [Alper Nabil Gabra Zakher](https://github.com/AlperNab)

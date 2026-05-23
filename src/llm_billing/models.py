from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional


class LedgerEntryType(str, Enum):
    CREDIT_ADD  = "credit_add"
    RESERVATION = "reservation"
    SETTLEMENT  = "settlement"
    RELEASE     = "release"
    ADJUSTMENT  = "adjustment"
    EXPIRY      = "expiry"


@dataclass
class LedgerEntry:
    id: str
    user_id: str
    entry_type: LedgerEntryType
    amount: Decimal
    balance_after: Decimal
    reservation_id: Optional[str]
    idempotency_key: Optional[str]
    metadata: dict
    created_at: datetime

    def __post_init__(self):
        self.amount = Decimal(str(self.amount))
        self.balance_after = Decimal(str(self.balance_after))


@dataclass
class Reservation:
    id: str
    user_id: str
    estimated_tokens: int
    estimated_cost: Decimal
    actual_tokens: Optional[int]
    actual_cost: Optional[Decimal]
    status: str
    idempotency_key: Optional[str]
    expires_at: datetime
    created_at: datetime
    settled_at: Optional[datetime]

    @property
    def is_pending(self) -> bool:
        return self.status == "pending"

    @property
    def savings(self) -> Optional[Decimal]:
        if self.actual_cost is None:
            return None
        return self.estimated_cost - self.actual_cost


@dataclass
class Balance:
    user_id: str
    available: Decimal
    reserved: Decimal
    total_added: Decimal
    total_spent: Decimal
    computed_at: datetime

    @property
    def total(self) -> Decimal:
        return self.available + self.reserved

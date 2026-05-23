class LLMBillingError(Exception):
    pass

class InsufficientCreditsError(LLMBillingError):
    def __init__(self, user_id: str, required, available):
        self.user_id = user_id
        self.required = required
        self.available = available
        super().__init__(
            f"User {user_id} has {available} credits but needs {required}"
        )

class ReservationNotFoundError(LLMBillingError):
    def __init__(self, reservation_id: str):
        super().__init__(f"Reservation {reservation_id} not found or already settled")

class DuplicateOperationError(LLMBillingError):
    def __init__(self, idempotency_key: str):
        super().__init__(f"Operation with key '{idempotency_key}' already processed")

class ReservationExpiredError(LLMBillingError):
    def __init__(self, reservation_id: str):
        super().__init__(f"Reservation {reservation_id} has expired")

"""Domain-specific failures."""


class StrategyOSError(Exception):
    """Base error for Strategy OS."""


class ValidationError(StrategyOSError):
    """A record or argument failed contract validation."""


class NotFoundError(StrategyOSError):
    """A scoped record does not exist."""


class ConflictError(StrategyOSError):
    """An optimistic revision or idempotency check failed."""


class ImmutableRecordError(StrategyOSError):
    """An immutable record already exists."""


class InvalidTransitionError(StrategyOSError):
    """A lifecycle transition is not permitted."""

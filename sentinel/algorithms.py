"""Pure rate-limit algorithm reference implementations (Phase 3)."""

TOKENS_PER_TOKEN_MICRO = 1_000_000


def tokens_to_micro(tokens: int) -> int:
    if tokens < 0:
        raise ValueError("tokens must be non-negative")
    return tokens * TOKENS_PER_TOKEN_MICRO


def micro_to_tokens(micro: int) -> int:
    if micro < 0:
        raise ValueError("micro must be non-negative")
    return micro // TOKENS_PER_TOKEN_MICRO


def token_bucket_evaluate(
    capacity_micro: int,
    refill_rate_micro_per_sec: int,
    tokens_micro: int,
    last_refill_micro: int,
    now_micro: int,
) -> tuple[bool, int, int]:
    elapsed = max(0, now_micro - last_refill_micro)
    refill = elapsed * refill_rate_micro_per_sec // TOKENS_PER_TOKEN_MICRO
    tokens = min(capacity_micro, tokens_micro + refill)
    allowed = tokens >= TOKENS_PER_TOKEN_MICRO
    if allowed:
        tokens -= TOKENS_PER_TOKEN_MICRO
        last_refill_micro = now_micro
    return allowed, tokens, last_refill_micro


def sliding_window_evaluate(
    limit: int,
    current_count: int,
    previous_count: int,
    window_start_micro: int,
    window_size_micro: int,
    now_micro: int,
) -> bool:
    if limit <= 0:
        raise ValueError("limit must be positive")
    if window_size_micro <= 0:
        raise ValueError("window_size_micro must be positive")
    elapsed = now_micro - window_start_micro
    if elapsed >= 2 * window_size_micro:
        current_count = 0
        previous_count = 0
        remaining_micro = window_size_micro
    elif elapsed >= window_size_micro:
        previous_count = current_count
        current_count = 0
        # The request arrives partway through the new window: only the time
        # since the new window began (elapsed - window_size_micro) has been
        # consumed, so the previous window still influences the estimate for
        # the remainder of the new window.
        remaining_micro = 2 * window_size_micro - elapsed
    else:
        remaining_micro = window_size_micro - elapsed
    estimated_scaled = current_count * window_size_micro + previous_count * remaining_micro
    return estimated_scaled < limit * window_size_micro

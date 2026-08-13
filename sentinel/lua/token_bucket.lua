-- Sentinel token bucket (Phase 4).
-- KEYS[1] = "tokens_micro:last_refill_micro" (missing or corrupt = fresh bucket)
-- ARGV[1] = capacity_micro (server-resolved from Policy, never client input)
-- ARGV[2] = refill_rate_micro_per_sec
-- Returns {allowed, tokens_after, last_refill_after, ttl_seconds}.
-- The decision and arithmetic mirror sentinel.algorithms.token_bucket_evaluate
-- exactly for every reachable state (Policy bounds guarantee double exactness).
-- Denied requests never write: the key and its TTL are untouched.

local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])

local now = redis.call("TIME")
local now_micro = now[1] * 1000000 + now[2]

local state = redis.call("GET", KEYS[1])
local tokens = capacity
local last_refill = now_micro
if state then
  local stored_tokens, stored_refill = string.match(state, "^(%d+):(%d+)$")
  if stored_tokens then
    tokens = tonumber(stored_tokens)
    last_refill = tonumber(stored_refill)
  end
end

local elapsed = now_micro - last_refill
if elapsed < 0 then
  elapsed = 0
end
local refill = math.floor(elapsed * rate / 1000000)
tokens = tokens + refill
if tokens > capacity then
  tokens = capacity
end

local allowed = 0
local ttl = -1
if tokens >= 1000000 then
  allowed = 1
  tokens = tokens - 1000000
  last_refill = now_micro
  -- %.0f keeps epoch-microsecond timestamps in decimal form; plain
  -- concatenation would emit scientific notation (1.7e+15) and corrupt the
  -- state on the next read.
  redis.call("SET", KEYS[1], string.format("%.0f:%.0f", tokens, last_refill))
  if rate > 0 then
    local refill_micro = capacity - tokens
    ttl = math.floor((refill_micro + rate - 1) / rate) + 1
    redis.call("EXPIRE", KEYS[1], ttl)
  end
end

return {allowed, tokens, last_refill, ttl}

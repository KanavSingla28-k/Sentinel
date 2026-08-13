-- Sentinel sliding window counter (Phase 4).
-- KEYS[1] = "current_count:previous_count:window_start_micro"
--           (missing or corrupt = fresh window)
-- ARGV[1] = limit (server-resolved from Policy, never client input)
-- ARGV[2] = window_size_micro
-- Returns {allowed, current_after, previous_after, window_start_after,
--          ttl_seconds}.
-- The decision mirrors sentinel.algorithms.sliding_window_evaluate exactly;
-- the returned counters are the post-rollover state the caller must persist.
-- The key expires after 2 windows, which is the rollover horizon: expiry is
-- lossless. Denied requests never write: the key and its TTL are untouched.

local limit = tonumber(ARGV[1])
local window_size = tonumber(ARGV[2])

local now = redis.call("TIME")
local now_micro = now[1] * 1000000 + now[2]

local state = redis.call("GET", KEYS[1])
local current = 0
local previous = 0
local window_start = now_micro
if state then
  local stored_current, stored_previous, stored_start =
      string.match(state, "^(%d+):(%d+):(%d+)$")
  if stored_current then
    current = tonumber(stored_current)
    previous = tonumber(stored_previous)
    window_start = tonumber(stored_start)
  end
end

local elapsed = now_micro - window_start
local remaining = window_size
if elapsed >= 2 * window_size then
  current = 0
  previous = 0
elseif elapsed >= window_size then
  previous = current
  current = 0
else
  remaining = window_size - elapsed
end

local allowed = 0
local ttl = -1
if current * window_size + previous * remaining < limit * window_size then
  allowed = 1
  current = current + 1
  -- %.0f keeps epoch-microsecond timestamps in decimal form; plain
  -- concatenation would emit scientific notation (1.7e+15) and corrupt the
  -- state on the next read.
  redis.call("SET", KEYS[1], string.format("%.0f:%.0f:%.0f", current, previous, window_start))
  ttl = math.floor((2 * window_size + 999999) / 1000000)
  redis.call("EXPIRE", KEYS[1], ttl)
end

return {allowed, current, previous, window_start, ttl}

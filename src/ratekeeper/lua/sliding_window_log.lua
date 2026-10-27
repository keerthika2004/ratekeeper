-- Sliding window log: remember the admission time of every unit, and count
-- how many fall inside the last `window` microseconds. Exact, but memory per
-- identity grows with the limit (which is why core/rules.py caps it lower).
--
-- KEYS[2i-1] = ZSET: score = admission time (us), member = unique sequence number
-- KEYS[2i]   = counter that hands out those sequence numbers
--
-- Members come from a counter, not from the timestamp or math.random():
-- two requests can share a timestamp, and Redis seeds math.random() the same
-- way on every script run, so either choice can produce duplicate members,
-- and ZADD with an existing member silently overwrites instead of adding.

local evals = {}
local all_allowed = true

for i = 1, NBANDS do
    local limit, window = band(i)
    local log_key = KEYS[2 * i - 1]
    -- A unit admitted at time s is inside the window while s > NOW - window.
    -- Trimming expired entries changes no decision, so it's safe even when
    -- this request ends up rejected.
    redis.call('ZREMRANGEBYSCORE', log_key, '-inf', int(NOW - window))
    local used = redis.call('ZCARD', log_key)
    local allowed = used + COST <= limit
    if not allowed then
        all_allowed = false
    end
    evals[i] = { limit = limit, window = window, used = used, allowed = allowed }
end

local out = { all_allowed and 1 or 0 }
for i = 1, NBANDS do
    local e = evals[i]
    local log_key = KEYS[2 * i - 1]
    local seq_key = KEYS[2 * i]
    if all_allowed then
        local last = redis.call('INCRBY', seq_key, COST)
        for seq = last - COST + 1, last do
            redis.call('ZADD', log_key, int(NOW), int(seq))
        end
        e.used = e.used + COST
        expire_ms(log_key, ceil_div(e.window, 1000))
        expire_ms(seq_key, ceil_div(e.window, 1000))
    end

    -- Fully refilled once the newest entry leaves the window.
    local reset_after = 0
    if e.used > 0 then
        local newest = redis.call('ZRANGE', log_key, -1, -1, 'WITHSCORES')
        reset_after = tonumber(newest[2]) + e.window - NOW
    end

    -- Rejected: wait until enough of the oldest entries expire to fit COST.
    -- The entry at 0-based rank (need - 1) is the last one that has to go.
    local retry_after = 0
    if not e.allowed then
        local need = e.used + COST - e.limit
        local entry = redis.call('ZRANGE', log_key, need - 1, need - 1, 'WITHSCORES')
        retry_after = tonumber(entry[2]) + e.window - NOW
    end
    push_result(out, e.allowed, e.limit, math.max(0, e.limit - e.used), reset_after, retry_after)
end
return out

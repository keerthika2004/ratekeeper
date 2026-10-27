-- Sliding window counter: estimate the last `window` of traffic by blending
-- two fixed-window counters,
--
--     estimate = prev * (time left in current window / window) + cur
--
-- which assumes last window's traffic was spread evenly. O(1) memory per
-- band, slightly approximate. Works in whole milliseconds, which keeps
-- limit * window below 2^53 (see MAX_LIMIT in core/rules.py); windows are whole
-- milliseconds anyway.
--
-- KEYS[i] = HASH { w = current window index, cur = units in window w, prev = units in window w - 1 }
--
-- The comparison is multiplied through by `window` so it stays in integers:
--     prev * weight + (cur + COST) * window <= limit * window

local now_ms = floor_div(NOW, 1000)
-- How far NOW is past the start of its millisecond. Subtracted from waits so
-- they end exactly on the millisecond where the decision changes.
local sub_ms = NOW - now_ms * 1000

local evals = {}
local all_allowed = true

for i = 1, NBANDS do
    local limit, window_us = band(i)
    local window = window_us / 1000
    local win = floor_div(now_ms, window)
    local elapsed = now_ms - win * window
    local stored = redis.call('HMGET', KEYS[i], 'w', 'cur', 'prev')
    local w = tonumber(stored[1])
    local cur, prev = 0, 0
    if w == win then
        cur = tonumber(stored[2])
        prev = tonumber(stored[3])
    elseif w == win - 1 then
        prev = tonumber(stored[2])
    end
    local weight = window - elapsed
    local allowed = prev * weight + (cur + COST) * window <= limit * window
    if not allowed then
        all_allowed = false
    end
    evals[i] = { limit = limit, window = window, win = win, elapsed = elapsed, weight = weight,
                 cur = cur, prev = prev, allowed = allowed }
end

local out = { all_allowed and 1 or 0 }
for i = 1, NBANDS do
    local e = evals[i]
    if all_allowed then
        e.cur = e.cur + COST
        redis.call('HSET', KEYS[i], 'w', int(e.win), 'cur', int(e.cur), 'prev', int(e.prev))
        -- Must survive through the next window, where it serves as `prev`.
        expire_ms(KEYS[i], 2 * e.window)
    end

    local remaining = math.max(0,
        floor_div(e.limit * e.window - e.prev * e.weight - e.cur * e.window, e.window))

    -- The estimate reaches zero once both counters have aged out:
    -- `cur` needs the rest of this window plus all of the next one.
    local reset_ms = 0
    if e.cur > 0 then
        reset_ms = 2 * e.window - e.elapsed
    elseif e.prev > 0 then
        reset_ms = e.weight
    end
    local reset_after = 0
    if reset_ms > 0 then
        reset_after = reset_ms * 1000 - sub_ms
    end

    local retry_after = 0
    if not e.allowed then
        local wait_ms
        if e.cur + COST <= e.limit then
            -- Fits once prev's weight shrinks enough, within this window.
            wait_ms = e.weight - floor_div((e.limit - e.cur - COST) * e.window, e.prev)
        else
            -- Only fits in the next window, once cur (then prev) has faded enough.
            wait_ms = e.weight + math.max(0, e.window - floor_div((e.limit - COST) * e.window, e.cur))
        end
        retry_after = wait_ms * 1000 - sub_ms
    end
    push_result(out, e.allowed, e.limit, remaining, reset_after, retry_after)
end
return out

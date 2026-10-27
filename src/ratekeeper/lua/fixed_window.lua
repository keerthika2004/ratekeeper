-- Fixed window: one counter per band, reset at every window boundary.

--

-- KEYS[i] = HASH { w = index of the window the count belongs to, c = units used in it }

--

-- Storing the window index inside the hash (instead of creating a new key per

-- window) keeps the key name fixed, so it can be declared in KEYS, which

-- Redis Cluster requires for every key a script touches.


 

local evals = {}

local all_allowed = true


 

-- Phase 1: evaluate every band without writing anything.

for i = 1, NBANDS do

    local limit, window = band(i)

    local win = floor_div(NOW, window)

    local stored = redis.call('HMGET', KEYS[i], 'w', 'c')

    local used = 0

    if tonumber(stored[1]) == win then

        used = tonumber(stored[2])

    end

    local allowed = used + COST <= limit

    if not allowed then

        all_allowed = false

    end

    evals[i] = { limit = limit, win = win, used = used, allowed = allowed, window_end = (win + 1) * window }

end


 

-- Phase 2: charge every band only if every band had room (all-or-nothing).

local out = { all_allowed and 1 or 0 }

for i = 1, NBANDS do

    local e = evals[i]

    if all_allowed then

        e.used = e.used + COST

        redis.call('HSET', KEYS[i], 'w', int(e.win), 'c', int(e.used))

        expire_ms(KEYS[i], ceil_div(e.window_end - NOW, 1000))

    end


 

    local reset_after = 0

    if e.used > 0 then

        reset_after = e.window_end - NOW

    end

    local retry_after = 0

    if not e.allowed then

        retry_after = e.window_end - NOW

    end

    push_result(out, e.allowed, e.limit, math.max(0, e.limit - e.used), reset_after, retry_after)

end

return out


 
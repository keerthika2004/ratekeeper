-- GCRA (Generic Cell Rate Algorithm): a token bucket stored as one number.

--

-- Each unit "costs" `interval` microseconds of quota (window / limit). The

-- band stores TAT, the theoretical arrival time: the moment the bucket would

-- be full again if nothing else arrived. A request is admitted if, after

-- adding its cost, TAT is at most `interval * burst` ahead of now; that slack

-- is what allows a burst of up to `burst` units at once.

--

-- KEYS[i] = STRING: TAT in microseconds


 

local evals = {}

local all_allowed = true


 

for i = 1, NBANDS do

    local limit, window, burst = band(i)

    -- Rounded up: the sustained rate never exceeds limit/window (never over-admits).

    local interval = ceil_div(window, limit)

    local tat = tonumber(redis.call('GET', KEYS[i])) or NOW

    local new_tat = math.max(tat, NOW) + interval * COST

    local allowed = new_tat - NOW <= interval * burst

    if not allowed then

        all_allowed = false

    end

    evals[i] = { burst = burst, interval = interval, tat = tat, new_tat = new_tat, allowed = allowed }

end


 

local out = { all_allowed and 1 or 0 }

for i = 1, NBANDS do

    local e = evals[i]

    if all_allowed then

        e.tat = e.new_tat

        redis.call('SET', KEYS[i], int(e.new_tat))

        expire_ms(KEYS[i], ceil_div(e.new_tat - NOW, 1000))

    end


 

    -- How far TAT is ahead of now = how much quota is currently in use.

    local ahead = math.max(e.tat, NOW) - NOW

    local remaining = math.max(0, floor_div(e.interval * e.burst - ahead, e.interval))


 

    local retry_after = 0

    if not e.allowed then

        retry_after = e.new_tat - e.interval * e.burst - NOW

    end

    push_result(out, e.allowed, e.burst, remaining, ahead, retry_after)

end

return out


 
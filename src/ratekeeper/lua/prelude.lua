-- prelude.lua -- shared helpers. scripts.py prepends this file to every

-- algorithm script before sending it to Redis, so each script is one atomic

-- unit: Redis runs it start to finish with no other command in between.

--

-- Calling convention (identical for every algorithm):

--   ARGV[1]   current time in microseconds, or '' to use Redis' own clock

--   ARGV[2]   cost: units of quota this request consumes

--   ARGV[3]   number of bands

--   ARGV[4..] three values per band: limit, window in microseconds, burst

--   KEYS      each band's keys, in band order (built by core/keys.py)

--

-- Returns a flat array of integers:

--   { all_allowed, then per band: allowed, limit, remaining, reset_after_us, retry_after_us }

--

-- Every number is an integer. Lua 5.1 stores numbers as doubles, which hold

-- every integer below 2^53 exactly; core/rules.py keeps all values and

-- products below that. Divisions go through floor_div/ceil_div so results

-- stay integers and match the Python reference model bit for bit.


 

-- A non-empty ARGV[1] means a test is driving a simulated clock.

local SIMULATED_CLOCK = ARGV[1] ~= ''


 

local function now_micros()

    if SIMULATED_CLOCK then

        return tonumber(ARGV[1])

    end

    -- Redis' clock, not the app's: several app instances may disagree about

    -- the time, but there is only one Redis clock per key.

    local t = redis.call('TIME')

    return tonumber(t[1]) * 1000000 + tonumber(t[2])

end


 

local NOW = now_micros()

local COST = tonumber(ARGV[2])

local NBANDS = tonumber(ARGV[3])


 

local function band(i)

    local base = 4 + (i - 1) * 3

    return tonumber(ARGV[base]), tonumber(ARGV[base + 1]), tonumber(ARGV[base + 2])

end


 

-- Integer to string without losing digits. tostring() formats with '%.14g',

-- which would turn the timestamp 1786532313123456 into '1.7865323131235e+15'.

local function int(n)

    return string.format('%d', n)

end


 

local function floor_div(a, b)

    return math.floor(a / b)

end


 

local function ceil_div(a, b)

    return math.floor((a + b - 1) / b)

end


 

-- Keys get a TTL so idle identities don't stay in Redis forever. TTLs run on

-- Redis' real clock, which a simulated test clock can't move, so they are

-- skipped in simulated mode (the algorithms never rely on expiry for

-- correctness; it only frees memory).

local function expire_ms(key, ms)

    if not SIMULATED_CLOCK then

        redis.call('PEXPIRE', key, int(ms))

    end

end


 

local function push_result(out, allowed, limit, remaining, reset_after, retry_after)

    out[#out + 1] = allowed and 1 or 0

    out[#out + 1] = limit

    out[#out + 1] = remaining

    out[#out + 1] = reset_after

    out[#out + 1] = retry_after

end


 
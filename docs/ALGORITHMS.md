# The four algorithms, in plain words


 

Every algorithm answers the same question: *"this identity wants to do something

that costs N units. Is there room right now?"* They differ in how they remember the

past, and that changes how fair, how exact and how memory-hungry they are.


 

All examples use **limit = 5 per 1 second**.


 

---


 

## 1. Fixed window: a counter that resets on the clock


 

**Idea.** Cut time into fixed slices (0-1s, 1-2s, ...). Count requests in the

current slice. Reject once the count reaches the limit. At the next slice the

count starts again from zero.


 

**Stored per identity:** one small hash: `{window number, count}`.


 

**The flaw: twice the limit at the edges.** Send 5 requests at 0.999s and 5 more at

1.000s. They land in different slices, so all 10 are allowed within one

millisecond. `test_fixed_window_lets_twice_the_limit_through_across_a_boundary` in `tests/test_reference_examples.py`

shows this.


 

**Also:** every identity's window resets at the same instant, so clients that

were waiting all retry together.


 

**Use it when:** you need something very cheap and roughly right, e.g. "1000 per

day" for a free tier, where 2x at midnight doesn't matter.


 

---


 

## 2. Sliding window log: a guest book


 

**Idea.** Write down the exact time of every admitted unit. To decide, throw away

entries older than 1 second and count what's left.


 

**Stored per identity:** one entry per admitted unit (a Redis sorted set), plus a

counter that gives each entry a unique id.


 

**Strength: exact.** In *any* 1-second stretch, never more than 5. It also knows the

exact wait time: when the oldest entry leaves the window, there's room again.


 

**Cost: memory grows with the limit.** A limit of 10,000 can mean 10,000 stored

entries per identity. That's why `core/rules.py` caps this algorithm's limit at

10,000 (the others allow 1,000,000).


 

**Use it when:** limits are small and exactness matters, e.g. "5 login attempts per

15 minutes".


 

> **Bug we avoid here:** each entry needs a unique name. Using the timestamp is

> not enough (two requests can arrive in the same microsecond), and using

> `math.random()` is worse: Redis seeds it identically on every script run,

> so it returns the same "random" number every time. A duplicate name silently

> overwrites the old entry, the count comes out too low, and the limiter

> over-admits. We use a per-identity counter (`INCRBY`) instead.


 

---


 

## 3. Sliding window counter: a good estimate


 

**Idea.** Keep the fixed-window counters for the current and the previous window.

Estimate the last second of traffic by assuming the previous window's requests

were spread evenly:


 

```

estimate = previous_count * (fraction of the current window still to go) + current_count

```


 

Example: the previous window had 10, and we're 30% into the current one with 2

so far. The estimate is `10 * 0.7 + 2 = 9`.


 

**Stored per identity:** three numbers. Memory is O(1) whatever the limit.


 

**Trade-off: approximate.** If last window's traffic all came at its very end,

the estimate is too low; if it all came at the start, too high. In practice the

error is small (Cloudflare measured 0.003% of requests wrongly decided on real

traffic), and it removes fixed window's 2x edge problem.


 

**Implementation detail:** the comparison is multiplied through by the window

length so it stays in whole numbers:

`prev * time_left + (cur + cost) * window <= limit * window`.


 

**Use it when:** you want close-to-exact limits with big numbers ("10,000 per

minute") where the log would cost too much memory.


 

---


 

## 4. GCRA: a token bucket stored as one timestamp


 

**Token bucket first.** Picture a bucket that holds up to `burst` tokens and refills

at `limit / window` tokens per second. Each request takes `cost` tokens; with

no tokens left, it's rejected. A user who has been idle has a full bucket and

can spend it at once (a burst), then has to wait for the refill rate.


 

**GCRA is the same thing, stored more cleverly.** Instead of storing "tokens left"

and "last refill time" and updating both, GCRA stores one number: the

**theoretical arrival time (TAT)**, the moment the bucket would be full again.


 

- Each unit "costs" `interval = window / limit` of time (here 200 ms).

- A request is allowed if, after adding its cost, the TAT is no more than

  `interval * burst` ahead of now.

- The exact wait falls straight out: `retry_after = new_TAT - interval * burst - now`.


 

Example with burst = 3: three requests at once are fine (TAT moves 600 ms ahead).

A fourth would push it 800 ms ahead, past the 600 ms allowed, so it waits 200 ms.


 

**Stored per identity:** one integer.


 

**Rounding is on the safe side:** `interval` is rounded *up* to a whole microsecond, so

the long-run rate can be a hair below the limit but never above it.


 

**Use it when:** in doubt. It's exact, O(1), allows controlled bursts (`burst`), and

gives smooth pacing (set `burst = 1` for "evenly spaced, no bursts at all").


 

> **Why no separate token bucket?** GCRA *is* a token bucket. They admit exactly

> the same requests. Implementing both would only duplicate code and tests.


 

---


 

## Side by side


 

| | Exact? | Memory per identity | Bursts | Good for |

|---|---|---|---|---|

| Fixed window | No: up to 2x at edges | 1 hash | Uncontrolled at edges | Cheap, coarse daily quotas |

| Sliding log | Yes | 1 entry per unit (up to `limit`) | None beyond the limit | Small, strict limits (logins, OTPs) |

| Sliding counter | Very close | 1 hash | Smoothed | Large limits where exactness is "nice to have" |

| GCRA | Yes | 1 integer | Controlled by `burst` | The default choice |


 

## Bands: several limits at once


 

A policy can stack up to 5 **bands**, for example 10 per second **and** 1,000 per

hour. A request must fit in every band. If any band says no, **no band is

charged**: the script checks all bands first, then writes all or nothing. Charging

the bands that said yes would use up hourly quota for requests that never

happened.


 

## What every algorithm reports


 

| Field | Meaning |

|---|---|

| `allowed` | Was the request admitted? |

| `limit` | The band's capacity (the burst for GCRA) |

| `remaining` | Units still available right now |

| `resetAfter` | Time until the band is completely refilled |

| `retryAfter` | If rejected: exactly how long until the same request would fit |


 

`retryAfter` and `resetAfter` are **exact to the microsecond**, and property tests

prove it: retrying at `now + retryAfter` succeeds, and retrying 1 µs earlier fails.


 
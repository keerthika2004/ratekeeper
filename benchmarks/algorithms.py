"""What each algorithm costs inside Redis: memory per identity and time per check.

Calls RedisRateLimiter directly (no HTTP), so the numbers are the algorithms'
own cost, not the web framework's.

   docker compose up -d redis
   uv run python benchmarks/algorithms.py

Uses Redis database 15 and EMPTIES it before and after. Never point it at real data.
"""

from __future__ import annotations

import argparse
import asyncio
import time

import redis.asyncio as redis

from ratekeeper.core.keys import redis_keys
from ratekeeper.core.models import Algorithm, Band, Policy
from ratekeeper.core.rules import normalize
from ratekeeper.redis_limiter import RedisRateLimiter

LIMITS = (10, 100, 1_000, 10_000)
HOUR_MS = 3_600_000


def make_policy(name: str, algorithm: Algorithm, limit: int) -> Policy:
   return Policy(name, 1, normalize(algorithm, None, [Band(limit, HOUR_MS)]))


async def memory_per_identity(
   client: redis.Redis, limiter: RedisRateLimiter, algorithm: Algorithm, limit: int
) -> int:
   """Bytes Redis holds for one identity whose band is completely full."""
   policy = make_policy(f"mem-{algorithm.script_name}-{limit}", algorithm, limit)
   for _ in range(limit):
       await limiter.check(policy, "user", 1)
   total = 0
   for key in redis_keys(policy, "user"):
       total += await client.memory_usage(key, samples=0) or 0  # samples=0: count every element exactly
   return total


async def cost_per_check(
   client: redis.Redis, limiter: RedisRateLimiter, algorithm: Algorithm, calls: int
) -> tuple[float, float, float]:
   """Returns (Redis CPU us per call, round-trip p50 us, round-trip p99 us).

   The CPU figure is Redis' own measurement of the script (INFO commandstats),
   so it excludes the network. The round trip is what the app actually waits.
   """
   policy = make_policy(f"lat-{algorithm.script_name}", algorithm, 1_000)
   for i in range(500):  # warm-up: loads the script into Redis, warms connections
       await limiter.check(policy, f"user-{i % 1_000}", 1)
   await client.config_resetstat()

   round_trips = []
   for i in range(calls):
       started = time.perf_counter()
       await limiter.check(policy, f"user-{i % 1_000}", 1)
       round_trips.append(time.perf_counter() - started)

   stats = (await client.info("commandstats"))["cmdstat_evalsha"]
   round_trips.sort()
   p50 = round_trips[len(round_trips) // 2] * 1e6
   p99 = round_trips[int(len(round_trips) * 0.99)] * 1e6
   return float(stats["usec_per_call"]), p50, p99


async def main() -> None:
   parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
   parser.add_argument("--redis-url", default="redis://localhost:6379/15")
   parser.add_argument("--calls", type=int, default=5_000, help="sequential checks per algorithm")
   args = parser.parse_args()

   client = redis.from_url(args.redis_url)
   limiter = RedisRateLimiter(client)
   await client.flushdb()
   try:
       print("## Memory per identity (bytes, band completely full)\n")
       print("| Algorithm | " + " | ".join(f"limit {n:,}" for n in LIMITS) + " |")
       print("|---|" + "---:|" * len(LIMITS))
       for algorithm in Algorithm:
           sizes = [await memory_per_identity(client, limiter, algorithm, n) for n in LIMITS]
           print(f"| {algorithm.value} | " + " | ".join(f"{size:,}" for size in sizes) + " |")

       print(f"\n## Cost per check ({args.calls:,} sequential calls per algorithm)\n")
       print("| Algorithm | Redis CPU per call (us) | Round trip p50 (us) | Round trip p99 (us) |")
       print("|---|---:|---:|---:|")
       for algorithm in Algorithm:
           cpu, p50, p99 = await cost_per_check(client, limiter, algorithm, args.calls)
           print(f"| {algorithm.value} | {cpu:.1f} | {p50:.0f} | {p99:.0f} |")
   finally:
       await client.flushdb()
       await client.aclose()


if __name__ == "__main__":
   asyncio.run(main())
 
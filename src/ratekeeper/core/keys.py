"""Redis key layout for one (policy, identity) pair.


 

    rk:{<policy>:<identity>}:v<version>:b<band>[:log|:seq]


 

- The policy name is in the key, so two policies never share a counter.

- The version is in the key, so editing a policy starts fresh counters.

- The ``{...}`` part is a Redis Cluster "hash tag": only that part decides which

  cluster node owns the key. All bands of one identity therefore live on the

  same node, which a multi-key Lua script requires.

- The identity is percent-encoded so a caller can't inject ``{``, ``}`` or ``:``

  to break the hash tag or collide with another key.

"""


 

from __future__ import annotations


 

from urllib.parse import quote


 

from ratekeeper.core.models import Algorithm, Policy



 

def redis_keys(policy: Policy, identity: str) -> list[str]:

    prefix = f"rk:{{{policy.name}:{quote(identity, safe='')}}}:v{policy.version}:b"

    keys: list[str] = []

    for band in range(len(policy.bands)):

        if policy.algorithm is Algorithm.SLIDING_WINDOW_LOG:

            keys += [f"{prefix}{band}:log", f"{prefix}{band}:seq"]

        else:

            keys.append(f"{prefix}{band}")

    return keys


 
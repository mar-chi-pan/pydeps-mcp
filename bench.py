import asyncio
import sys
import time

import httpx
from packaging.utils import canonicalize_name

import server


async def run(path: str, limit: int) -> tuple[int, float]:
    deps = server.read_dependencies(path)
    names = list(
        dict.fromkeys(canonicalize_name(d["name"]) for d in deps["dependencies"])
    )
    sem = asyncio.Semaphore(limit)
    async with httpx.AsyncClient(timeout=10) as client:
        start = time.perf_counter()
        await asyncio.gather(
            *(server._fetch_limited(client, sem, n) for n in names),
            return_exceptions=True,
        )
        return len(names), time.perf_counter() - start


for limit in (1, 10):
    n, secs = asyncio.run(run(sys.argv[1], limit))
    print(f"concurrency={limit}: {n} packages in {secs:.2f}s")

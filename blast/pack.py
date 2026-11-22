"""Split tests over workers: longest first onto the least loaded worker (LPT),
trying 1 to 8 workers and keeping the cheapest (busiest worker + 2 s each)."""

WORKER_MS = 2000
MAX_WORKERS = 8


def lpt(nums, ms, k):
    loads = [0] * k
    bins = [[] for _ in range(k)]
    for n in sorted(nums, key=lambda n: -ms[n]):
        i = loads.index(min(loads))
        bins[i].append(n)
        loads[i] += ms[n]
    return bins, max(loads)


def cost(workers, ms):
    workers = [w for w in workers if w]
    if not workers:
        return 0
    return max(sum(ms[n] for n in w) for w in workers) + WORKER_MS * len(workers)


def pack(nums, ms):
    if not nums:
        return []
    best, best_cost = None, None
    for k in range(1, min(MAX_WORKERS, len(nums)) + 1):
        bins, top = lpt(nums, ms, k)
        c = top + WORKER_MS * k
        if best_cost is None or c < best_cost:
            best, best_cost = bins, c
    return [b for b in best if b]


def cost_durs(durs):
    """Minimum packed cost (ms) for a sequence of test durations in ms."""
    if not durs:
        return 0
    ordered = sorted(durs, reverse=True)
    n = len(ordered)
    best = ordered[0] + WORKER_MS if n == 1 else sum(ordered) + WORKER_MS
    for k in range(2, min(MAX_WORKERS, n) + 1):
        loads = [0] * k
        for d in ordered:
            i = loads.index(min(loads))
            loads[i] += d
        c = max(loads) + WORKER_MS * k
        if c < best:
            best = c
    return best

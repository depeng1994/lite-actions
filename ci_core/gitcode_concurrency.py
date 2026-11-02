"""Two independent read operations, joined before any authorization decision."""

from concurrent.futures import ThreadPoolExecutor


def read_pair(first, second, *, enabled):
    if not enabled:
        return first(), second()
    # The context joins both tasks even if the first result raises. No request
    # survives into a later poll, and no nested executor is used by callers.
    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(first)
        right = pool.submit(second)
        return left.result(), right.result()

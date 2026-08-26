"""quote_bridge.py — cache-first decision logic for quote reads hitting
broker_proxy's POST /call route. Pure function, no Flask dependency.
"""

CACHE_MISS = object()

QUOTE_METHODS = frozenset({"get_quotes", "get_quotes_safe"})


def serve_quote_from_cache(feed, method_name, args, kwargs, max_age_sec=None):
    """Return a cached quote dict, or CACHE_MISS to signal REST RPC fallback."""
    if feed is None or method_name not in QUOTE_METHODS:
        return CACHE_MISS

    args = list(args or [])
    kwargs = dict(kwargs or {})
    if len(args) >= 2:
        exchange, token = args[0], args[1]
    elif "exchange" in kwargs and "token" in kwargs:
        exchange, token = kwargs["exchange"], kwargs["token"]
    else:
        return CACHE_MISS

    try:
        quote = feed.get_quote(exchange, token, max_age_sec=max_age_sec)
    except Exception:
        return CACHE_MISS
    if quote is None:
        return CACHE_MISS
    return quote

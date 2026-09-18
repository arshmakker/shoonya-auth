"""quote_bridge.py — cache-first decision logic for quote reads hitting
broker_proxy's POST /call route. Pure function, no Flask dependency.
"""

CACHE_MISS = object()

QUOTE_METHODS = frozenset({"get_quotes", "get_quotes_safe"})


def quote_instrument(args, kwargs):
    """(exchange, token) from a quote call's args/kwargs, or None if absent."""
    args = list(args or [])
    kwargs = dict(kwargs or {})
    if len(args) >= 2:
        return args[0], args[1]
    if "exchange" in kwargs and "token" in kwargs:
        return kwargs["exchange"], kwargs["token"]
    return None


def serve_quote_from_cache(feed, method_name, args, kwargs, max_age_sec=None):
    """Return a cached quote dict, or CACHE_MISS to signal REST RPC fallback."""
    if feed is None or method_name not in QUOTE_METHODS:
        return CACHE_MISS

    instrument = quote_instrument(args, kwargs)
    if instrument is None:
        return CACHE_MISS
    exchange, token = instrument

    try:
        quote = feed.get_quote(exchange, token, max_age_sec=max_age_sec)
    except Exception:
        return CACHE_MISS
    if quote is None or "lp" not in quote:
        # No fresh last-price: TickStore drops stale fields individually, so
        # a quote can come back with fresh depth but no fresh (or any) lp.
        # get_quotes/get_quotes_safe callers need a price — fall back to REST
        # rather than serve depth-only data as if it answered the call.
        return CACHE_MISS
    return quote

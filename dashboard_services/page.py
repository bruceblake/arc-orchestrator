"""Offset pagination for one list inside an existing JSON object.

Callers that omit both `limit` and `offset` are not paging. The payload
is returned unchanged so phone.html and today's tests keep the full list.
"""


def clamp_int(raw, default, lo, hi):
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    if n < lo:
        return default if default >= lo else lo
    return hi if n > hi else n


def requested(query, *, default_limit, max_limit):
    """parse_qs dict → (limit, offset), or None when the client did not page."""
    if not query or ("limit" not in query and "offset" not in query):
        return None
    limit = clamp_int((query.get("limit") or [str(default_limit)])[0],
                       default_limit, 1, max_limit)
    offset = clamp_int((query.get("offset") or ["0"])[0], 0, 0, 1_000_000)
    if offset < 0:
        offset = 0
    return limit, offset


def apply(payload, key, limit, offset):
    """Shallow-copy `payload`, slice `payload[key]`, and attach `page`.

    The input dict and its list are not mutated. `next_offset` is None on
    the last page.
    """
    items = list(payload.get(key) or [])
    total = len(items)
    if offset < 0:
        offset = 0
    window = items[offset:offset + limit]
    nxt = offset + limit if offset + limit < total else None
    out = dict(payload)
    out[key] = window
    out["page"] = {
        "key": key,
        "limit": limit,
        "offset": offset,
        "total": total,
        "next_offset": nxt,
    }
    return out

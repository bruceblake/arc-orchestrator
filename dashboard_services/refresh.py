"""ETag for a poll payload.

Clock fields are ignored so a poll that only aged 'now' or a duration
can answer 304. Event timestamps (`ts` nested under a list) stay in the
hash: they identify the row. Only a top-level `now` or `ts` is a
response clock.
"""

import hashlib
import json

# Durations that grow with wall time. The browser ticks these locally.
_DURATIONS = frozenset({"idle_s", "last_event_s", "seconds"})


def _strip(obj, top=True):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if top and k in ("now", "ts"):
                continue
            if k in _DURATIONS:
                continue
            out[k] = _strip(v, top=False)
        return out
    if isinstance(obj, list):
        return [_strip(v, top=False) for v in obj]
    return obj


def revision(obj):
    raw = json.dumps(_strip(obj), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def etag_for(obj):
    return '"' + revision(obj) + '"'


def not_modified(client_etag, obj):
    if not client_etag:
        return False
    return client_etag == etag_for(obj)

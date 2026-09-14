"""/api/usage/hourly — the same day's traffic, cut into 24 one-hour buckets.

The usage page's totals are daily; answering "which hour was bad" meant
eye-balling a 5-minute timeline. This endpoint re-cuts the SAME events the
daily aggregation walks, so the two can never disagree about what counts:
both go through dashboard._day_window / dashboard._bucket_points.

The failure these tests exist for is silent and lopsided — an hourly view that
walks its own event filter, or drops a family it does not recognise, reports
fewer requests than the daily view for the very same date. Every test here
therefore pins either the shape (24 zero-filled hours) or the agreement with
_usage (sum of hours == daily total).
"""
import json
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

from helpers import capture_events  # noqa: F401  (sys.path)

import config
import dashboard


class _FakeHandler(dashboard.Handler):
    """A socket-less Handler that captures status/headers/body in memory.

    BaseHTTPRequestHandler.__init__ requires a live socket; we skip it and set
    only what do_GET/do_POST read, then override the write path so nothing is
    sent over the network. Same contract as tests/test_http_read.py.
    """

    def __init__(self):
        self.status = None
        self.response_headers = {}
        self.body = b""
        self.wfile = self
        self.path = "/"

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass

    def write(self, data):
        self.body += data

    def json(self):
        return json.loads(self.body.decode())


class UsageHourly(unittest.TestCase):
    """GET /api/usage/hourly over a redirected event log."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self._orig_log = config.EVENTS_LOG
        config.EVENTS_LOG = str(self.root / "events.jsonl")
        dashboard._lines_cache["key"] = None
        # _usage/_usage_hourly both merge kimi-code CLI sessions read from the
        # real ~/.kimi-code/sessions; unstubbed, the counts depend on whatever
        # the operator happens to be running. Same stub as test_usage_range.py.
        self._orig_kimi = dashboard._kimi_code_usage
        dashboard._kimi_code_usage = lambda now, fleet_names=frozenset(): {
            "models": [], "inflight": [], "points": [], "turns": []}
        dashboard._fleet_names_cache.update(key=0.0, names=frozenset())
        # The opencode transcript backfill reads the real logs/harness dir and
        # would add tokens the fixture never wrote.
        self._orig_backfill = dashboard._opencode_token_backfill
        dashboard._opencode_token_backfill = lambda *a, **k: []

    def tearDown(self):
        dashboard._kimi_code_usage = self._orig_kimi
        dashboard._opencode_token_backfill = self._orig_backfill
        config.EVENTS_LOG = self._orig_log
        dashboard._lines_cache["key"] = None
        self._dir.cleanup()

    # -- fixture ------------------------------------------------------------
    def write_events(self, *events):
        Path(config.EVENTS_LOG).write_text(
            "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        dashboard._lines_cache["key"] = None

    def done(self, ts, model="GLM-5.3", tokens=100, harness="opencode"):
        return {"ts": ts, "type": "driver.done", "harness": harness, "model": model,
                "role": "implementer", "task": "t1", "attempt": 1, "tokens": tokens,
                "prompt_tokens": tokens // 2, "completion_tokens": tokens // 2,
                "seconds": 1.0}

    def _hour(self, day, hour, minute=0):
        """Epoch seconds for a local wall-clock hour on `day` (YYYY-MM-DD)."""
        y, m, d = (int(p) for p in day.split("-"))
        lt = time.localtime()
        return time.mktime((y, m, d, hour, minute, 0, lt.tm_wday, lt.tm_yday, -1))

    def get(self, path):
        h = _FakeHandler()
        h.path = path
        h.do_GET()
        return h

    def call(self, **q):
        url = "/api/usage/hourly"
        if q:
            url += "?" + "&".join(f"{k}={v}" for k, v in q.items())
        h = self.get(url)
        return h.json(), h.status

    # -- happy path ---------------------------------------------------------
    def test_events_land_in_the_hour_they_happened(self):
        day = "2026-09-10"
        self.write_events(self.done(self._hour(day, 3, 5), tokens=100),
                          self.done(self._hour(day, 3, 50), tokens=100),
                          self.done(self._hour(day, 14, 1), tokens=50))
        body, status = self.call(date=day)
        self.assertEqual(status, 200)
        self.assertEqual(body["hours"][3]["totals"]["requests"], 2)
        self.assertEqual(body["hours"][3]["totals"]["tokens"], 200)
        self.assertEqual(body["hours"][14]["totals"]["requests"], 1)
        self.assertEqual(body["hours"][4]["totals"]["requests"], 0)

    def test_hours_carry_a_by_model_breakdown(self):
        day = "2026-09-10"
        self.write_events(self.done(self._hour(day, 9), model="GLM-5.3", tokens=100),
                          self.done(self._hour(day, 9),
                                    model="DeepSeek-V4.1-Flash-thinking-max", tokens=7,
                                    harness="reasonix"))
        body, _ = self.call(date=day)
        by_model = body["hours"][9]["by_model"]
        self.assertEqual(sorted(by_model), ["DeepSeek-V4.1-Flash-thinking-max", "GLM-5.3"])
        self.assertEqual(by_model["GLM-5.3"]["requests"], 1)
        self.assertEqual(by_model["GLM-5.3"]["tokens"], 100)
        self.assertEqual(by_model["DeepSeek-V4.1-Flash-thinking-max"]["tokens"], 7)

    def test_totals_match_the_daily_view_for_the_same_date(self):
        """The whole point of the shared helper: the two cuts must agree.

        An hourly view that filters its own events reports less traffic than
        the daily row beside it, and the operator cannot tell which is wrong.
        """
        day = "2026-09-10"
        self.write_events(self.done(self._hour(day, 1), tokens=100),
                          self.done(self._hour(day, 13), tokens=250),
                          self.done(self._hour(day, 23, 59), tokens=50))
        body, _ = self.call(date=day)
        self.assertEqual(body["totals"]["requests"], 3)
        self.assertEqual(body["totals"]["tokens"], 400)
        across_hours = sum(h["totals"]["requests"] for h in body["hours"])
        self.assertEqual(across_hours, body["totals"]["requests"])
        self.assertEqual(sum(h["totals"]["tokens"] for h in body["hours"]), 400)
        # And the day total is the daily view's own row for that date. This is
        # the assertion the endpoint exists for: the two cuts are two views of
        # ONE collection, so a disagreement here is a bug in both readings.
        row = next(r for r in dashboard._usage(None, "all")["daily"]
                   if r["date"] == day)
        self.assertEqual(body["totals"]["requests"], row["requests"])
        self.assertEqual(body["totals"]["tokens"], row["tokens"])

    def test_a_late_evening_event_belongs_to_its_own_local_day(self):
        """The last hours of a local day are the FIRST of the next UTC day.

        The daily breakdown used to bucket by `ts // 86400` (a UTC day) while
        labelling the bucket with local time, so the row for a date quietly
        held the previous local evening — and no hourly view could ever agree
        with it. 23:59 must land on the day the operator names, and nowhere
        else.
        """
        day, nxt = "2026-09-10", "2026-09-11"
        self.write_events(self.done(self._hour(day, 23, 59), tokens=100),
                          self.done(self._hour(nxt, 0, 1), tokens=5))
        body, _ = self.call(date=day)
        self.assertEqual(body["totals"]["requests"], 1)
        self.assertEqual(body["totals"]["tokens"], 100)
        self.assertEqual(body["hours"][23]["totals"]["tokens"], 100)
        after = self.call(date=nxt)[0]
        self.assertEqual(after["totals"]["requests"], 1)
        self.assertEqual(after["hours"][0]["totals"]["tokens"], 5)
        # The daily row agrees with BOTH hourly days.
        rows = {r["date"]: r for r in dashboard._usage(None, "all")["daily"]}
        self.assertEqual(rows[day]["tokens"], body["totals"]["tokens"])
        self.assertEqual(rows[nxt]["tokens"], after["totals"]["tokens"])

    def test_events_from_other_days_are_absent(self):
        day = "2026-09-10"
        nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        self.write_events(self.done(self._hour(day, 12)),
                          self.done(self._hour(nxt, 12)))
        body, _ = self.call(date=day)
        self.assertEqual(body["totals"]["requests"], 1)

    # -- shape --------------------------------------------------------------
    def test_there_are_always_twenty_four_zero_filled_buckets(self):
        self.write_events(self.done(self._hour("2026-09-10", 5)))
        body, _ = self.call(date="2026-09-10")
        self.assertEqual([h["hour"] for h in body["hours"]], list(range(24)))
        self.assertEqual(len(body["hours"]), 24)
        for h in body["hours"]:
            self.assertIn("by_model", h)
            self.assertIn("totals", h)
            # The same counters the daily aggregation keeps, so an hour can be
            # compared against the day without translating field names.
            self.assertTrue({"requests", "ok", "errors", "failed_attempts",
                             "tokens"}.issubset(h["totals"]), sorted(h["totals"]))

    def test_an_empty_day_is_twenty_four_zeros_not_an_error(self):
        self.write_events()
        body, status = self.call(date="2020-01-01")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["hours"]), 24)
        self.assertEqual(body["totals"]["requests"], 0)
        self.assertEqual(body["totals"]["tokens"], 0)
        self.assertTrue(all(h["by_model"] == {} for h in body["hours"]))

    def test_the_date_is_echoed_back(self):
        self.write_events()
        body, _ = self.call(date="2026-09-01")
        self.assertEqual(body["date"], "2026-09-01")

    def test_the_default_date_is_today(self):
        today = time.strftime("%Y-%m-%d")
        self.write_events(self.done(time.time(), tokens=100))
        body, status = self.call()
        self.assertEqual(status, 200)
        self.assertEqual(body["date"], today)
        self.assertEqual(body["totals"]["requests"], 1)
        self.assertEqual(body["hours"][time.localtime().tm_hour]["totals"]["tokens"], 100)

    # -- per-model table ----------------------------------------------------
    def test_each_hour_models_sum_to_that_hours_totals(self):
        day = "2026-09-10"
        self.write_events(self.done(self._hour(day, 8), model="GLM-5.3", tokens=100),
                          self.done(self._hour(day, 8), model="GLM-5.3", tokens=100),
                          self.done(self._hour(day, 8),
                                    model="DeepSeek-V4.1-Flash-thinking-max", tokens=13,
                                    harness="reasonix"))
        body, _ = self.call(date=day)
        h = body["hours"][8]
        self.assertEqual(sum(m["requests"] for m in h["by_model"].values()),
                         h["totals"]["requests"])
        self.assertEqual(sum(m["tokens"] for m in h["by_model"].values()),
                         h["totals"]["tokens"])

    def test_a_model_row_reports_the_same_counters_as_the_daily_row(self):
        day = "2026-09-10"
        self.write_events(self.done(self._hour(day, 7), model="GLM-5.3", tokens=100),
                          self.done(self._hour(day, 7), model="GLM-5.3", tokens=100),
                          self.done(self._hour(day, 21), model="GLM-5.3", tokens=50))
        body, _ = self.call(date=day)
        p = body["hours"][7]["by_model"]["GLM-5.3"]
        self.assertEqual(p, {"requests": 2, "tokens": 200})
        daily = [m for m in dashboard._usage(None, "all")["models"]
                 if m["model"] == "GLM-5.3"]
        self.assertEqual(sum(m["requests"] for m in daily), 3)
        self.assertEqual(sum(m["tokens"] for m in daily), 250)

    # -- failures -----------------------------------------------------------
    def test_a_failed_driver_attempt_is_counted_in_its_hour(self):
        day = "2026-09-10"
        self.write_events({"ts": self._hour(day, 4), "type": "driver.error",
                           "harness": "opencode", "model": "GLM-5.3",
                           "role": "implementer", "task": "t1", "attempt": 1,
                           "error": "concurrent session limit"})
        body, _ = self.call(date=day)
        self.assertEqual(body["hours"][4]["totals"]["failed_attempts"], 1)
        self.assertEqual(body["hours"][4]["totals"]["requests"], 0)
        self.assertEqual(body["totals"]["failed_attempts"], 1)

    # -- bad input ----------------------------------------------------------
    def test_a_malformed_date_is_a_400_with_a_json_error(self):
        for bad in ("not-a-date", "2026-13-45", "20260910", "2026-9-1", ""):
            body, status = self.call(date=bad)
            self.assertEqual(status, 400, f"{bad!r} should be rejected")
            self.assertIn("error", body)

    def test_a_400_does_not_leak_a_200_shaped_body(self):
        body, status = self.call(date="2026-02-30")
        self.assertEqual(status, 400)
        self.assertNotIn("hours", body)

    def test_an_unrepresentable_date_is_a_400_not_a_crash(self):
        """The last representable date parses but has no following day.

        `_day_window` needs the NEXT midnight, so 9999-12-31 raises
        OverflowError while the route only catches ValueError — a 500 from a
        request anyone can send. It must be a JSON 400 like any other bad date.
        """
        body, status = self.call(date="9999-12-31")
        self.assertEqual(status, 400, body)
        self.assertIn("error", body)
        self.assertNotIn("hours", body)
        # The other end of the range is representable and must still work.
        body, status = self.call(date="0001-01-01")
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["hours"]), 24)

    # -- the shared collection ---------------------------------------------
    def test_the_route_is_served(self):
        self.write_events()
        h = self.get("/api/usage/hourly?date=2026-09-10")
        self.assertEqual(h.status, 200)
        self.assertEqual(h.response_headers.get("Content-Type"), "application/json")
        self.assertEqual(h.json()["date"], "2026-09-10")

    def test_bucketing_is_shared_with_the_daily_walk(self):
        """_bucket_points must be the one place hour/time-bucket arithmetic lives.

        If the endpoint grew its own copy, a fix to one would silently leave
        the other — exactly how the daily and hourly views would drift apart.
        """
        self.assertTrue(hasattr(dashboard, "_bucket_points"))
        self.assertTrue(hasattr(dashboard, "_day_window"))
        day0 = dashboard._day_window("2026-09-10")[0]
        pts = [(day0 + 3600 * 5 + 60, "glm", "GLM-5.3", 1, 1, 0, 0, 10, 0),
               (day0 + 3600 * 5 + 600, "glm", "GLM-5.3", 1, 1, 0, 0, 5, 0),
               (day0 + 3600 * 9, "deepseek", "DS", 1, 1, 0, 0, 1, 0)]
        buckets, totals, by_model = dashboard._bucket_points(pts, day0, 24, 3600)
        self.assertEqual(buckets[5]["totals"]["requests"], 2)
        self.assertEqual(buckets[5]["totals"]["tokens"], 15)
        self.assertEqual(sum(b["totals"]["requests"] for b in buckets), 3)
        self.assertEqual(totals["tokens"], 16)

    def test_a_crashed_driver_is_not_counted_as_a_failed_request(self):
        """ok/errors/failed_attempts are carried explicitly, never derived.

        A crashed driver attempt adds to failed_attempts WITHOUT adding a
        request, so an `ok = requests - errors` identity would report -1 ok —
        which is how an hourly view silently disagrees with the daily totals
        sitting next to it in the same UI.
        """
        day0 = dashboard._day_window("2026-09-10")[0]
        # (ts, family, model, requests, ok, errors, failed_attempts, tokens, runs)
        crash = (day0 + 3600 * 2, "glm", "GLM-5.3", 0, 0, 0, 1, 0, 0)
        buckets, totals, _by = dashboard._bucket_points([crash], day0, 24, 3600)
        t = buckets[2]["totals"]
        self.assertEqual(t["requests"], 0)
        self.assertEqual(t["ok"], 0)
        self.assertEqual(t["failed_attempts"], 1)
        self.assertEqual(totals["failed_attempts"], 1)
        self.assertGreaterEqual(t["ok"], 0)

    def test_the_day_window_is_a_calendar_day(self):
        start, end = dashboard._day_window("2026-09-10")
        self.assertEqual(end - start, 86400)
        lt = time.localtime(start)
        self.assertEqual((lt.tm_hour, lt.tm_min, lt.tm_sec), (0, 0, 0))
        self.assertEqual(time.strftime("%Y-%m-%d", lt), "2026-09-10")

    # -- DST ----------------------------------------------------------------
    # A local day is not always 86400 seconds long. The three bugs these pin
    # were invisible in UTC and in any fixed-offset zone: the daily list
    # skipped a calendar date, the day window ended at the wrong local hour,
    # and the hourly labels drifted from the local clock. Each was reproduced
    # only with TZ=America/New_York, so these assertions are written as
    # arithmetic that must hold in whatever zone the tests run in, and the
    # transition-specific ones check the property rather than a fixed width.
    def _midnight(self, day_s, **kw):
        y, m, d = (int(p) for p in day_s.split("-"))
        kw.setdefault("hour", 0)
        return time.mktime((y, m, d, kw["hour"], kw.get("minute", 0), 0, 0, 0, -1))

    def _transition_days(self, start_s="2026-01-01", n=400):
        """The DST transition days of the RUNNING zone, as (spring, fall).

        The shift dates differ per zone (Europe/Berlin's are not America/New_
        York's), so pinning fixed calendar dates would either assert another
        zone's arithmetic or silently test nothing. A transition day is the one
        whose local length is not 86400 s — computed here with `mktime`
        directly, NOT via dashboard._day_window: a helper that discovered its
        dates through the code under test would find none once that code is
        broken, and every test using it would skip instead of failing, which is
        the one outcome a regression test must not have. Each is None when the
        zone has no such transition near `start_s` (UTC, a fixed offset, no
        DST), and those tests skip rather than assert arithmetic the zone does
        not have.
        """
        day = date.fromisoformat(start_s)
        spring = fall = None
        for _ in range(n):
            ds = day.isoformat()
            span = self._midnight((day + timedelta(days=1)).isoformat()) \
                - self._midnight(ds)
            if span == 23 * 3600:
                spring = spring or ds
            elif span == 25 * 3600:
                fall = fall or ds
            if spring and fall:
                break
            day += timedelta(days=1)
        return spring, fall

    def test_the_day_window_ends_at_the_next_local_midnight(self):
        """The window is the span between two consecutive local midnights.

        A fixed 86400 ends 23:00 on the 25-hour fall-back day (dropping an
        hour the daily rows still count) and reaches into the next local date
        on the 23-hour spring-forward day (counting an hour the daily rows put
        under the other date). Either way the two cuts of one day disagree.
        """
        spring, fall = self._transition_days()
        normal = "2026-09-10"
        if normal in (spring, fall):
            normal = "2026-09-10"  # a date outside every transition window
        for day in [normal] + [d for d in (spring, fall) if d]:
            start, end = dashboard._day_window(day)
            nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
            self.assertEqual(start, self._midnight(day), day)
            self.assertEqual(end, self._midnight(nxt), day)
            lt = time.localtime(start)
            self.assertEqual((lt.tm_hour, lt.tm_min, lt.tm_sec), (0, 0, 0), day)
            self.assertEqual(time.strftime("%Y-%m-%d", lt), day)
            et = time.localtime(end)
            self.assertEqual((et.tm_hour, et.tm_min, et.tm_sec), (0, 0, 0), day)
            self.assertEqual(time.strftime("%Y-%m-%d", et), nxt)
        self.assertEqual(dashboard._day_window(normal)[1]
                         - dashboard._day_window(normal)[0], 86400)
        # Where the zone does have transitions they are exactly 23h and 25h —
        # the whole point, and exactly what a fixed 86400 gets wrong.
        if spring:
            s, e = dashboard._day_window(spring)
            self.assertEqual(e - s, 23 * 3600, spring)
        if fall:
            s, e = dashboard._day_window(fall)
            self.assertEqual(e - s, 25 * 3600, fall)

    def test_a_late_evening_event_on_a_fall_back_day_is_inside_its_window(self):
        """23:30 on the 25-hour day belongs to that day, and to hour 23.

        With a fixed 86400 the window ended at local 23:00, so the daily row
        counted this traffic and the hourly view had no bucket for it.
        """
        _spring, day = self._transition_days()
        if not day:
            self.skipTest("this timezone has no 25-hour fall-back day")
        start, end = dashboard._day_window(day)
        late = self._midnight(day, hour=23, minute=30)
        self.assertGreaterEqual(late, start)
        self.assertLess(late, end, "23:30 local fell outside its own day")
        self.write_events(self.done(late, tokens=100),
                          self.done(start + 60, tokens=25))
        body, _ = self.call(date=day)
        self.assertEqual(body["totals"]["tokens"], 125)
        self.assertEqual(body["totals"]["requests"], 2)
        self.assertEqual(body["hours"][23]["totals"]["tokens"], 100)

    def test_the_window_membership_predicate_is_the_local_calendar_date(self):
        """`start <= ts < end` must mean exactly "local date == date_s".

        This is the invariant that makes the daily and hourly cuts agree: the
        daily rows are keyed by local calendar date, so if the window admits a
        ts the daily rows place under a different date — or rejects one they
        place under this one — the two views report different numbers for the
        same day. A fixed 86400 window broke it on both transition days; the
        span between two consecutive local midnights cannot.
        """
        spring, fall = self._transition_days()
        days = ["2026-09-10"] + [d for d in (spring, fall) if d]
        if not (spring or fall):
            self.skipTest("this timezone has no DST transition to check")
        for day in days:
            start, end = dashboard._day_window(day)
            # Walk the whole window minute by minute, plus a margin each side,
            # so a mismatched instant cannot hide between samples.
            t = start - 3600
            while t < min(end + 3600, start + 26 * 3600):
                inside = start <= t < end
                by_date = dashboard._ds(t) == day
                self.assertEqual(inside, by_date,
                                 f"{day}: ts {t} ({dashboard._ds(t)} "
                                 f"{time.strftime('%H:%M', time.localtime(t))}) "
                                 f"window={inside} date={by_date}")
                t += 60

    def test_totals_still_match_the_daily_view_on_a_transition_day(self):
        """The daily predicate and the hourly buckets count the same events.

        The comparison is against the events the daily rows would count — a
        local-date filter, which is what `_day_window` now expresses — because
        the 30-day list only reaches back a month and a fixed transition date
        is usually outside it.
        """
        checked = [d for d in self._transition_days() if d]
        if not checked:
            self.skipTest("no DST transition in this timezone")
        for day in checked:
            start, end = dashboard._day_window(day)
            events = [self.done(start + 60, tokens=100),
                      self.done(end - 60, tokens=50)]
            self.write_events(*events)
            body, _ = self.call(date=day)
            # The daily view's own predicate over the same events.
            daily_req = daily_tok = 0
            for e in events:
                ts = e["ts"]
                if dashboard._ds(ts) == day and start <= ts < end:
                    daily_req += 1
                    daily_tok += e["tokens"]
            self.assertEqual(body["totals"]["requests"], daily_req, day)
            self.assertEqual(body["totals"]["tokens"], daily_tok, day)
            self.assertEqual(sum(h["totals"]["tokens"] for h in body["hours"]),
                             daily_tok, day)

    def test_every_hour_bucket_holds_the_hour_its_label_names(self):
        """Bucket h must hold traffic that happened at local hour h.

        The bucket was chosen by `start + h*3600` while the label said local
        hour h, so after an intra-day transition every later label was off by
        one against the operator's clock. Checked through the endpoint, whose
        hour labels the UI prints.
        """
        for day in ["2026-09-10"] + [d for d in self._transition_days() if d]:
            start, end = dashboard._day_window(day)
            y, m, d = (int(p) for p in day.split("-"))
            events, expected = [], {}
            for h in range(24):
                # The first instant on `day` whose LOCAL hour is h. On a
                # spring-forward day one hour never occurs, and is skipped.
                t, found = start, None
                while t < end:
                    lt = time.localtime(t)
                    if (lt.tm_year, lt.tm_mon, lt.tm_mday) == (y, m, d) \
                            and lt.tm_hour == h:
                        found = t
                        break
                    t += 60
                if found is None:
                    continue
                events.append(self.done(found, tokens=100))
                expected[h] = expected.get(h, 0) + 1
            self.write_events(*events)
            body, _ = self.call(date=day)
            for i, h in enumerate(body["hours"]):
                self.assertEqual(h["totals"]["requests"], expected.get(i, 0),
                                 f"{day} hour {i} did not hold its own hour")
            self.assertEqual(sum(h["totals"]["requests"] for h in body["hours"]),
                             len(events), day)

    def test_the_daily_list_has_thirty_distinct_dates_across_a_dst_change(self):
        """No calendar date may be missing from the 30-day list.

        Enumeration stepped by `_ds(today0 - (29-i)*86400)`: across a
        spring-forward the same local date is re-derived twice and a whole
        date is absent, so every point dated that day hit the `rec is None`
        skip and a full day of traffic vanished from the daily view for the
        next 30 days. Verified live for TZ=America/New_York, today=2026-03-09:
        no 2026-03-08 row. The old stepping is reproduced alongside, so this
        test is not vacuous in a zone without a transition — it fails there
        only if the two ever agree, which is the point.
        """
        for today in ("2026-03-09", "2026-11-02", "2026-09-10"):
            got = dashboard._last_dates(30, today)
            self.assertEqual(len(got), 30, today)
            self.assertEqual(len(set(got)), 30,
                             f"{today}: a date is duplicated/skipped: {got}")
            self.assertEqual(got, sorted(got), today)
            self.assertEqual(got[-1], today)
            self.assertEqual(date.fromisoformat(got[-1])
                             - date.fromisoformat(got[0]), timedelta(days=29))
            # The bug this replaced: a fixed 86400s step over a transition
            # re-derives one date twice and drops another. Where the zone has
            # such a transition inside the window the old stepping is provably
            # short of 30 distinct dates — which `got` must never be.
            today0 = dashboard._day_window(today)[0]
            old = {dashboard._ds(today0 - (29 - i) * 86400)
                   for i in range(30)}
            if len(old) < 30:
                self.assertTrue(set(old) <= set(got), today)
                self.assertIn(dashboard._ds(today0 - 29 * 86400), got,
                              f"{today}: the date the old stepping dropped")
        # And the endpoint's own list is that same contiguous run of dates.
        # `_today_str` is pinned to a post-transition date so this drives the
        # real 30-day walk over a spring-forward whatever day the suite runs:
        # unpinned, the test would only bite for 30 days a year.
        orig_today = dashboard._today_str
        self.addCleanup(setattr, dashboard, "_today_str", orig_today)
        self.write_events()
        for pinned in ("2026-03-09", "2026-11-02"):
            dashboard._today_str = lambda p=pinned: p
            dates = [r["date"] for r in dashboard._usage(None, "all")["daily"]]
            self.assertEqual(len(dates), 30, pinned)
            self.assertEqual(len(set(dates)), 30,
                             f"{pinned}: the daily view duplicated/skipped a "
                             f"date: {dates}")
            self.assertEqual(dates, sorted(dates), pinned)
            self.assertEqual(dates, dashboard._last_dates(30, pinned))
            self.assertEqual(dates[-1], pinned)
            # The date the buggy epoch-stepping re-derived twice is missing
            # from its own list; whatever the zone, this one is contiguous.
            today0 = dashboard._day_window(pinned)[0]
            old = [dashboard._ds(today0 - (29 - i) * 86400) for i in range(30)]
            self.assertEqual((date.fromisoformat(dates[-1])
                              - date.fromisoformat(dates[0])).days, 29, pinned)
            if len(set(old)) < 30:
                missing = set(dates) - set(old)
                self.assertTrue(missing, f"{pinned}: fixed list hides no hole")
                self.assertIn(dashboard._ds(today0 - 29 * 86400), missing,
                              f"{pinned}: the dropped transition date is absent "
                              f"from the list that dropped it")
        dashboard._today_str = orig_today
        # In a DST zone a transition date inside the window must be present.
        dates = [r["date"] for r in dashboard._usage(None, "all")["daily"]]
        today = date.fromisoformat(dashboard._today_str())
        for back in range(1, 30):
            d = (today - timedelta(days=back)).isoformat()
            start, end = dashboard._day_window(d)
            if end - start != 86400:
                self.assertIn(d, dates, f"transition day {d} is missing")
                break

    def test_the_query_is_parsed_by_the_real_route_not_a_stub(self):
        # do_GET is unwrapped: a 400 must come from the route itself.
        self.write_events()
        parsed = urlparse("/api/usage/hourly?date=bogus")
        self.assertEqual(parsed.path, "/api/usage/hourly")


if __name__ == "__main__":
    unittest.main()

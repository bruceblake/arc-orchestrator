"""Tests for the humanize_seconds helper.

humanize_seconds turns a number of seconds into a short display string. These
tests pin down every branch: sub-minute, sub-hour, hours with one decimal, the
None case, and the negative-value error.
"""

import unittest

from helpers import capture_events  # noqa: F401  (sys.path)

import humanize


class TestHumanizeSeconds(unittest.TestCase):
    def test_under_sixty_seconds_renders_seconds_suffix(self):
        self.assertEqual(humanize.humanize_seconds(45), "45s")

    def test_the_sixty_second_boundary_is_one_minute(self):
        self.assertEqual(humanize.humanize_seconds(60), "1m")

    def test_under_an_hour_renders_minutes_suffix(self):
        self.assertEqual(humanize.humanize_seconds(720), "12m")

    def test_an_hour_is_one_point_zero_hours(self):
        self.assertEqual(humanize.humanize_seconds(3600), "1.0h")

    def test_hours_render_with_one_decimal(self):
        self.assertEqual(humanize.humanize_seconds(5400), "1.5h")

    def test_none_renders_dash(self):
        self.assertEqual(humanize.humanize_seconds(None), "-")

    def test_negative_raises_value_error(self):
        with self.assertRaises(ValueError):
            humanize.humanize_seconds(-1)


class TestHumanizeSecondsTypes(unittest.TestCase):
    def test_accepts_float_input(self):
        self.assertEqual(humanize.humanize_seconds(1.5), "1s")


if __name__ == "__main__":
    unittest.main()

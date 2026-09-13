"""Test package.

The suite is loaded two ways: `unittest discover -s tests -t tests` imports
each module top-level (the tests directory is on sys.path), while a gate that
loads `tests.test_config` by dotted name imports them as package members —
and then a bare `from helpers import ...` cannot resolve. Make the package
itself put this directory on sys.path so both spellings work.
"""
import pathlib
import sys

_HERE = str(pathlib.Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

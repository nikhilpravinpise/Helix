"""Correlation accuracy ground truth (subphase E7).

`events.metrics.correlation_accuracy` takes `expected_groups` as an
argument because there is no way to derive "these event ids should share
an incident" from the event store itself. This package is where that
ground truth is declared, so the metric can be run against a live
Postgres instead of only the unit-test suite.

See `groups.py` for the two correlation groups and where each event in
them comes from, and `run.py` for the CLI that ingests them and reports
the number.
"""

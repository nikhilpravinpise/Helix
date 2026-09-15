"""Controlled benchmark and evaluation harness (subphase 1.11).

The package drives scenarios under benchmark/scenarios/ through the real
CLI code paths (`helix intake`, `helix run`), applies the scripted
approver (decision D2), and scores outcomes per CONTEXT.md section 17.
See docs/benchmark/methodology.md for arm definitions, injection modes and
metric semantics.
"""

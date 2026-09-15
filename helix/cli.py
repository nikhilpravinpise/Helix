"""Helix CLI entry point.

Subcommands are added per subphase (see phase1-remaining-plan.md):
intake/run/approve/approvals/provenance/incidents land with 1.4-1.9,
serve with 1.4. This module is deliberately just the parser skeleton.
"""

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="helix",
        description="Helix - evidence-constrained DevOps recovery",
    )
    parser.add_subparsers(dest="command")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0

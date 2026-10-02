"""Command line entry point."""

from __future__ import annotations

import argparse
import sys

from .meta import LoadError, load

DESCRIPTION = """\
Inspect Iceberg table metadata and Parquet file metadata in a terminal UI.

TARGET can be:
  a Parquet file            data/events.parquet
  a directory of Parquet    data/warehouse/events/
  an Iceberg table dir      warehouse/db/events        (holds metadata/)
  an Iceberg metadata file  warehouse/db/events/metadata/v3.metadata.json
  a catalog table name      db.events --catalog prod
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="icefloor",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("target", help="path to inspect, or table name with --catalog")
    parser.add_argument(
        "-c",
        "--catalog",
        help="load TARGET as a table name from this configured PyIceberg catalog",
    )
    parser.add_argument(
        "-s",
        "--section",
        help="open on this section key (e.g. snapshots, datafiles, rowgroups)",
    )
    parser.add_argument(
        "--list-sections",
        action="store_true",
        help="print the section keys for TARGET and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        source = load(args.target, args.catalog)
    except LoadError as exc:
        print(f"icefloor: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"icefloor: could not load {args.target}: {exc}", file=sys.stderr)
        return 1

    if args.list_sections:
        for section in source.sections():
            print(f"{section.key}\t{section.label}")
        return 0

    from .app import IcefloorApp

    IcefloorApp(source, args.section).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EGM
"""
Dev check: can every entity a ServiceExecution references be read from
Stellio?

Reads a ServiceExecution (JSON) from a file or stdin, prints it, then
fetches and pretty-prints its entityId plus every NGSI-LD id (urn:ngsi-ld:...)
found anywhere inside its "input" - content-based, so it works whatever the
input field names or nesting (profiles, ControlPoints, WeatherInformation...).
Nothing is written; for the real input -> CAFLOOD job conversion, see
caflood_build.py.

Usage:
    python check_inputs.py ServiceExecution_relative_time.json
    cat payload.json | python check_inputs.py

Environment: STELLIO_* (see caflood_common.py / .env.example).
Exit codes: 0 = every entity resolved, 1 = some could not be fetched,
2 = configuration / JSON error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from caflood_common import Config, ConfigError, StellioClient


def find_entity_ids(node: Any, found: set[str]) -> None:
    """Recursively collect every string that looks like an NGSI-LD URN."""
    if isinstance(node, str):
        if node.startswith("urn:ngsi-ld:"):
            found.add(node)
    elif isinstance(node, dict):
        for value in node.values():
            find_entity_ids(value, found)
    elif isinstance(node, list):
        for item in node:
            find_entity_ids(item, found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_file", nargs="?", help="ServiceExecution JSON file (default: stdin)")
    args = parser.parse_args(argv)

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    raw = Path(args.input_file).read_text() if args.input_file else sys.stdin.read()
    try:
        execution = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Input is not valid JSON: {exc}", file=sys.stderr)
        return 2

    print("=" * 70)
    print("ServiceExecution (as received)")
    print("=" * 70)
    print(json.dumps(execution, indent=2))

    ids_to_fetch: set[str] = set()

    entity_id = execution.get("entityId")
    entity_type = execution.get("entityType")

    if isinstance(entity_id, str) and entity_id.startswith("urn:ngsi-ld:"):
        ids_to_fetch.add(entity_id)
    else:
        print(f"\n! entityId does not look like an NGSI-LD URN: {entity_id!r}", file=sys.stderr)

    if isinstance(entity_type, str) and entity_type.startswith("urn:ngsi-ld:"):
        print(
            f"! entityType looks like an id, not a type: {entity_type!r} "
            "(entityId / entityType are probably swapped in this payload)",
            file=sys.stderr,
        )
        ids_to_fetch.add(entity_type)

    find_entity_ids(execution.get("input", {}), ids_to_fetch)

    if not ids_to_fetch:
        print("\nNo NGSI-LD entity ids found to resolve.", file=sys.stderr)
        return 1

    stellio = StellioClient(config)
    resolved = 0
    failed: list[str] = []

    for eid in sorted(ids_to_fetch):
        print("\n" + "=" * 70)
        print(f"Entity: {eid}")
        print("=" * 70)
        try:
            entity = stellio.get_entity(eid)
            print(json.dumps(entity, indent=2))
            resolved += 1
        except Exception as exc:  # noqa: BLE001 - smoke test, report everything
            print(f"! failed to fetch {eid}: {exc}", file=sys.stderr)
            failed.append(eid)

    print("\n" + "-" * 70, file=sys.stderr)
    print(f"Resolved {resolved}/{len(ids_to_fetch)} entities.", file=sys.stderr)
    if failed:
        print(f"Failed: {failed}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

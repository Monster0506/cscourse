from __future__ import annotations

import argparse
from pathlib import Path

from schedule_builder.vsb import import_semester


def main() -> None:
    parser = argparse.ArgumentParser(description="Import Kent State CS lecture sections from VSB.")
    parser.add_argument("--term", required=True, help="VSB term identifier, such as 202680 for Fall 2026.")
    parser.add_argument("--output-dir", default="data", type=Path, help="Directory for generated term JSON files.")
    arguments = parser.parse_args()

    destination = arguments.output_dir / f"{arguments.term}.json"
    payload = import_semester(arguments.term, destination)
    print(
        f"Imported {len(payload['lectures'])} lectures for {payload['term']['name']} to {destination}. "
        f"{len(payload['unresolved_instructors'])} instructor emails were unavailable."
    )


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from schedule_builder.vsb import WEEKDAY_INDEX, format_days
from schedule_builder.web import group_room_sessions


FIELDNAMES = (
    "course",
    "title",
    "room",
    "meeting",
    "instructors",
    "emails",
)


def build_rows(lectures: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for session in group_room_sessions(lectures):
        instructors = ", ".join(speaker["name"] for speaker in session["speakers"])
        emails = ", ".join(speaker["email"] or "Unavailable" for speaker in session["speakers"])
        for meeting in session["meetings"]:
            rows.append(
                {
                    "course": session["course"],
                    "title": session["title"],
                    "room": session["room"],
                    "meeting": f"{meeting['start']}-{meeting['end']} {format_days(list(meeting['days']))}",
                    "instructors": instructors,
                    "emails": emails,
                    "_day_index": WEEKDAY_INDEX.get(meeting["days"][0], len(WEEKDAY_INDEX)) if meeting["days"] else len(WEEKDAY_INDEX),
                    "_start": meeting["start"],
                }
            )
    rows.sort(key=lambda row: (row["course"], row["_day_index"], row["_start"], row["room"]))
    for row in rows:
        del row["_day_index"]
        del row["_start"]
    return rows


def export_tsv(source: Path, destination: Path) -> int:
    with source.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    rows = build_rows(payload["lectures"])

    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export an imported Kent State CS schedule to TSV.")
    parser.add_argument("--term", required=True, help="Term id matching data/<term>.json, such as 202680.")
    parser.add_argument("--data-dir", default="data", type=Path, help="Directory containing the imported term JSON.")
    parser.add_argument("--output", type=Path, default=None, help="Destination TSV path (default: data/<term>.tsv).")
    arguments = parser.parse_args()

    source = arguments.data_dir / f"{arguments.term}.json"
    destination = arguments.output or arguments.data_dir / f"{arguments.term}.tsv"
    row_count = export_tsv(source, destination)
    print(f"Wrote {row_count} rows to {destination}")


if __name__ == "__main__":
    main()

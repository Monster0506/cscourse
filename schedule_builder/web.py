from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any
import json

from flask import Flask, abort, render_template, request


DAY_ORDER = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
DAY_INDEX = {day: index + 1 for index, day in enumerate(DAY_ORDER)}
TIME_RAIL = tuple(f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'}" for hour in range(7, 21))


def minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def layout_overlapping_events(events: list[dict[str, Any]]) -> None:
    """
    Assign horizontal lanes to overlapping events and allow each event
    to expand into unused lanes.

    Adds:
        column          - zero-based starting lane
        column_count    - total lanes in the overlap group
        column_span     - number of lanes this event may occupy
        left_percent    - horizontal starting position
        width_percent   - maximum available width
    """
    if not events:
        return

    events.sort(
        key=lambda event: (
            minutes(event["start"]),
            minutes(event["end"]),
            event["course"],
        )
    )

    # Split the day's events into connected overlap groups.
    groups: list[list[dict[str, Any]]] = []
    current_group: list[dict[str, Any]] = []
    group_end = -1

    for event in events:
        start = minutes(event["start"])
        end = minutes(event["end"])

        if current_group and start >= group_end:
            groups.append(current_group)
            current_group = []
            group_end = -1

        current_group.append(event)
        group_end = max(group_end, end)

    if current_group:
        groups.append(current_group)

    for group in groups:
        # First assign each event to the leftmost available lane.
        column_ends: list[int] = []
        columns: list[list[dict[str, Any]]] = []

        for event in group:
            start = minutes(event["start"])
            end = minutes(event["end"])

            column = None

            for index, column_end in enumerate(column_ends):
                if start >= column_end:
                    column = index
                    column_ends[index] = end
                    break

            if column is None:
                column = len(column_ends)
                column_ends.append(end)
                columns.append([])

            event["column"] = column
            columns[column].append(event)

        column_count = len(columns)

        # Then let every event expand into empty columns to its right.
        for event in group:
            event_start = minutes(event["start"])
            event_end = minutes(event["end"])

            span = 1

            for column_index in range(event["column"] + 1, column_count):
                blocked = False

                for other in columns[column_index]:
                    other_start = minutes(other["start"])
                    other_end = minutes(other["end"])

                    # Half-open intervals:
                    # 10:00-11:00 does not overlap 11:00-12:00.
                    if event_start < other_end and other_start < event_end:
                        blocked = True
                        break

                if blocked:
                    break

                span += 1

            event["column_count"] = column_count
            event["column_span"] = span
            event["left_percent"] = event["column"] / column_count * 100
            event["width_percent"] = span / column_count * 100


def group_room_sessions(lectures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sessions: dict[tuple[Any, ...], dict[str, Any]] = {}
    for lecture in lectures:
        meetings = tuple(
            sorted(
                (meeting for meeting in lecture["meetings"] if meeting["day"] in DAY_INDEX),
                key=lambda meeting: (DAY_INDEX[meeting["day"]], meeting["start"]),
            )
        )
        if not meetings:
            continue
        meeting_key = tuple((meeting["day"], meeting["start"], meeting["end"]) for meeting in meetings)
        key = (lecture["course"], lecture["title"], lecture["campus"], lecture["room"], meeting_key)
        session = sessions.setdefault(
            key,
            {
                "course": lecture["course"],
                "title": lecture["title"],
                "campus": lecture["campus"],
                "room": lecture["room"],
                "meetings": meetings,
                "sections": [],
                "speakers": [],
            },
        )
        section = {"section": lecture["section"], "crn": lecture["crn"]}
        speaker = {"name": lecture["instructor"] or "Unavailable", "email": lecture["email"]}
        if section not in session["sections"]:
            session["sections"].append(section)
        if speaker not in session["speakers"]:
            session["speakers"].append(speaker)

    return sorted(
        sessions.values(),
        key=lambda session: (
            session["course"],
            DAY_INDEX[session["meetings"][0]["day"]],
            session["meetings"][0]["start"],
            session["room"],
        ),
    )


def create_app(data_directory: Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DATA_DIRECTORY"] = data_directory or Path("data")

    @app.get("/")
    def index() -> str:
        directory = Path(app.config["DATA_DIRECTORY"])
        files = sorted(directory.glob("*.json")) if directory.exists() else []
        print(files)
        available_terms: list[dict[str, str]] = []
        for file in files:
            with file.open(encoding="utf-8") as source:
                payload = json.load(source)
            term = payload.get("term", {})
            if isinstance(term.get("id"), str) and isinstance(term.get("name"), str):
                available_terms.append({"id": term["id"], "name": term["name"]})

        selected = request.args.get("term") or (available_terms[0]["id"] if available_terms else None)
        if selected is None:
            return render_template("schedule.html", terms=[], schedule=None, calendar={}, rows=[], hours=TIME_RAIL)
        if selected not in {term["id"] for term in available_terms}:
            abort(404)

        with (directory / f"{selected}.json").open(encoding="utf-8") as source:
            schedule = json.load(source)
        sessions = group_room_sessions(schedule["lectures"])
        calendar: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for session in sessions:
            for meeting in session["meetings"]:
                calendar[meeting["day"]].append(
                    {
                        **session,
                        **meeting,
                        "start_slot": max(1, (minutes(meeting["start"]) - 420) // 15 + 1),
                        "duration_slots": max(1, (minutes(meeting["end"]) - minutes(meeting["start"]) + 14) // 15),
                    }
                )

        for day in DAY_ORDER:
            calendar[day].sort(
                key=lambda event: (
                    event["start"],
                    event["end"],
                    event["course"],
                    event["room"],
                )
            )
            layout_overlapping_events(calendar[day])

        # for day in DAY_ORDER:
        #    calendar[day].sort(key=lambda event: (event["start"], event["course"], event["room"]))
        return render_template(
            "schedule.html",
            terms=available_terms,
            schedule=schedule,
            calendar=calendar,
            rows=sessions,
            days=DAY_ORDER,
            hours=TIME_RAIL,
        )

    return app


app = create_app()

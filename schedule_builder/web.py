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
            calendar[day].sort(key=lambda event: (event["start"], event["course"], event["room"]))
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

from __future__ import annotations

from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener
import json
import re
import time
import xml.etree.ElementTree as ElementTree


BASE_URL = "https://schedulebuilder.kent.edu/"
FACULTY_DIRECTORY_URL = "https://www.kent.edu/cs/faculty-staff"

DAY_NAMES = {
    "1": "Sunday",
    "2": "Monday",
    "3": "Tuesday",
    "4": "Wednesday",
    "5": "Thursday",
    "6": "Friday",
    "7": "Saturday",
}

INCLUDED_CAMPUSES = {"KC"}
INCLUDED_BLOCK_TYPES = {"Lec", "Combined L"}

WEEKDAY_ORDER = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
WEEKDAY_INDEX = {day: index for index, day in enumerate(WEEKDAY_ORDER)}
DAY_LETTERS = {"Monday": "M", "Tuesday": "T", "Wednesday": "W", "Thursday": "R", "Friday": "F"}


def format_days(days: list[str]) -> str:
    ordered = sorted(days, key=lambda day: WEEKDAY_INDEX.get(day, len(WEEKDAY_ORDER)))
    return "/".join(DAY_LETTERS.get(day, day) for day in ordered)


def cache_buster(now_ms: int | None = None) -> dict[str, str]:
    minute = int(
        (time.time() * 1000 if now_ms is None else now_ms) // 60_000
    ) % 1000

    return {
        "t": str(minute),
        "e": str(minute % 3 + minute % 39 + minute % 42),
    }


def normalize_instructor_name(name: str) -> str:
    name = name.strip()

    if "," in name:
        last, first = (part.strip() for part in name.split(",", 1))
        name = f"{first} {last}"

    name = re.sub(
        r"\b(?:dr|phd|ph\.d|prof)\b\.?",
        "",
        name,
        flags=re.IGNORECASE,
    )

    return " ".join(
        re.sub(r"[^A-Za-z0-9 ]", " ", name).casefold().split()
    )


def match_key(normalized_name: str) -> tuple[str, str]:
    tokens = normalized_name.split()
    if not tokens:
        return ("", "")
    return (tokens[0], tokens[-1])


def resolve_email(normalized_instructor: str, faculty_emails: dict[str, str]) -> str | None:
    if normalized_instructor in faculty_emails:
        return faculty_emails[normalized_instructor]
    key = match_key(normalized_instructor)
    for name, email in faculty_emails.items():
        if match_key(name) == key:
            return email
    return None


def is_undergraduate_course(course: str) -> bool:
    prefix, separator, number = course.partition(" ")

    return (
        prefix == "CS"
        and bool(separator)
        and number.isdecimal()
        and 10_000 <= int(number) < 50_000
    )


def clock_time(minutes: str) -> str:
    value = int(minutes)
    return f"{value // 60:02d}:{value % 60:02d}"


class FacultyDirectoryParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()

        self._link_href: str | None = None
        self._link_text: list[str] = []
        self._latest_name: str | None = None
        self.emails: dict[str, str] = {}

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag == "a":
            self._link_href = dict(attrs).get("href")
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._link_href is not None:
            self._link_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._link_href is None:
            return

        text = " ".join("".join(self._link_text).split())

        if self._link_href.startswith("mailto:"):
            email = self._link_href.removeprefix("mailto:").split("?", 1)[0]

            if self._latest_name and email.endswith("@kent.edu"):
                self.emails[
                    normalize_instructor_name(self._latest_name)
                ] = email

        elif text:
            self._latest_name = text

        self._link_href = None
        self._link_text = []


def parse_faculty_directory(document: str) -> dict[str, str]:
    parser = FacultyDirectoryParser()
    parser.feed(document)
    return parser.emails


def room_time_groups(
    block: dict[str, str],
    timeblocks: dict[str, dict[str, str]],
) -> dict[str, tuple[tuple[str, str, str], ...]]:
    """Group a block's own timeblocks by room, keeping each room's
    weekly (day, start, end) pattern. A plain single-room block has an
    empty `loos` and collapses to one room holding every timeblock."""
    loos = json.loads(block.get("loos") or "{}")
    location = block.get("location", "")
    ids = [identifier for identifier in block.get("timeblockids", "").split(",") if identifier]

    by_room: dict[str, list[tuple[str, str, str]]] = {}
    for identifier in ids:
        timeblock = timeblocks.get(identifier)
        if timeblock is None:
            continue
        day = DAY_NAMES.get(timeblock.get("day", ""), "Unknown")
        if day in {"Saturday", "Sunday"}:
            continue
        room = loos.get(identifier, location) if loos else location
        entry = (day, clock_time(timeblock["t1"]), clock_time(timeblock["t2"]))
        by_room.setdefault(room, []).append(entry)

    return {room: tuple(sorted(entries)) for room, entries in by_room.items()}


def pick_lecture_room(
    groups: dict[str, tuple[tuple[str, str, str], ...]],
    pattern_counts: dict[tuple[str, tuple[tuple[str, str, str], ...]], int],
    course_code: str,
    section: str,
    room_overrides: dict[str, str],
) -> tuple[str, bool]:
    """Pick which room-group is the lecture. A single-room block has one
    group and is unambiguous. A combined block's lecture time is normally
    identical across every section (one shared lecture, N separate lab
    breakouts) - the room+time pattern seen in the most sections wins.
    Only when that is also tied (typically a single-section course) does
    a manual override apply."""
    if len(groups) <= 1:
        return next(iter(groups), ""), False

    within_counts = {room: len(times) for room, times in groups.items()}
    max_within = max(within_counts.values())
    leaders = [room for room, count in within_counts.items() if count == max_within]

    if len(leaders) == 1:
        return leaders[0], False

    cross_section_counts = {room: pattern_counts.get((room, groups[room]), 0) for room in leaders}
    max_cross = max(cross_section_counts.values())
    cross_leaders = [room for room, count in cross_section_counts.items() if count == max_cross]

    if len(cross_leaders) == 1:
        return cross_leaders[0], False

    override_room = room_overrides.get(f"{course_code}|{section}")
    if override_room in cross_leaders:
        return override_room, False

    return "", True


def parse_class_data(
    document: str,
    faculty_emails: dict[str, str],
    room_overrides: dict[str, str] | None = None,
    ambiguous_sink: list[str] | None = None,
    skip_sections: set[str] | None = None,
) -> list[dict[str, Any]]:
    room_overrides = room_overrides or {}
    skip_sections = skip_sections or set()
    root = ElementTree.fromstring(document)
    course = root.find(".//course")
    offering = root.find(".//offering")

    if course is None or offering is None:
        raise ValueError(
            "VSB class-data response has no course offering"
        )

    course_code = f"{course.attrib['code']} {course.attrib['number']}"
    title = offering.attrib.get("title", "")

    included_blocks: list[tuple[dict[str, str], dict[str, tuple[tuple[str, str, str], ...]]]] = []
    pattern_counts: dict[tuple[str, tuple[tuple[str, str, str], ...]], set[str]] = {}

    for uselection in course.findall("uselection"):
        timeblocks = {
            block.attrib["id"]: block.attrib
            for block in uselection.findall("timeblock")
        }

        for selection in uselection.findall("selection"):
            for block in selection.findall("block"):
                block_type = block.attrib.get("type", "")
                campus = block.attrib.get("campus", "")

                if (
                    block_type not in INCLUDED_BLOCK_TYPES
                    or campus not in INCLUDED_CAMPUSES
                ):
                    continue

                groups = room_time_groups(block.attrib, timeblocks)
                if not groups:
                    continue
                crn = block.attrib["key"]
                for room, times in groups.items():
                    pattern_counts.setdefault((room, times), set()).add(crn)

                included_blocks.append((block.attrib, groups))

    pattern_section_counts = {key: len(crns) for key, crns in pattern_counts.items()}

    records: dict[str, dict[str, Any]] = {}
    meetings_by_time: dict[str, dict[tuple[str, str], set[str]]] = {}

    for block, groups in included_blocks:
        crn = block["key"]
        instructor = block.get("teacher", "").strip() or None
        normalized = normalize_instructor_name(instructor) if instructor else ""
        section = block.get("secNo", "")

        lecture_room, ambiguous = pick_lecture_room(
            groups, pattern_section_counts, course_code, section, room_overrides
        )

        if ambiguous:
            if f"{course_code}|{section}" not in skip_sections and ambiguous_sink is not None:
                ambiguous_sink.append(f"{course_code} section {section}")
            continue

        record = records.setdefault(
            crn,
            {
                "course": course_code,
                "title": title,
                "section": section,
                "crn": crn,
                "instructor": instructor,
                "email": (
                    resolve_email(normalized, faculty_emails)
                    if normalized
                    else None
                ),
                "campus": block.get("campus", ""),
                "component": block.get("type", ""),
                "room": lecture_room,
                "meetings": [],
            },
        )
        times = meetings_by_time.setdefault(crn, {})

        for day, start, end in groups[lecture_room]:
            times.setdefault((start, end), set()).add(day)

    for crn, record in records.items():
        record["meetings"] = [
            {
                "days": [day for day in WEEKDAY_ORDER if day in days],
                "start": start,
                "end": end,
            }
            for (start, end), days in sorted(meetings_by_time.get(crn, {}).items())
        ]

    return sorted(
        (
            record
            for record in records.values()
            if record["meetings"]
        ),
        key=lambda record: (
            record["course"],
            record["section"],
            record["crn"],
        ),
    )


class VsbClient:
    def __init__(self) -> None:
        self._opener = build_opener(
            HTTPCookieProcessor(CookieJar())
        )
        self._get("criteria.jsp")

    def _get(
        self,
        endpoint: str,
        parameters: dict[str, str] | None = None,
    ) -> str:
        url = f"{BASE_URL}{endpoint}"

        if parameters:
            url = f"{url}?{urlencode(parameters)}"

        request = Request(
            url,
            headers={
                "User-Agent": "kent-cs-schedule-builder/1",
            },
        )

        with self._opener.open(request, timeout=30) as response:
            return response.read().decode("utf-8")

    def _post(
        self,
        endpoint: str,
        parameters: dict[str, str],
    ) -> Any:
        request = Request(
            f"{BASE_URL}{endpoint}",
            data=urlencode(parameters).encode(),
            headers={
                "Content-Type": (
                    "application/x-www-form-urlencoded"
                ),
                "User-Agent": "kent-cs-schedule-builder/1",
            },
        )

        with self._opener.open(request, timeout=30) as response:
            return json.loads(
                response.read().decode("utf-8")
            )

    def terms(self) -> dict[str, str]:
        document = self._get("criteria.jsp")

        matches = re.findall(
            r'"(\d+)":\{"name":"([^"]+)"',
            document,
        )

        return dict(matches)

    def course_codes(
        self,
        term: str,
        term_name: str,
    ) -> list[str]:
        codes: set[str] = set()
        page = 0

        while True:
            document = self._get(
                "api/courses/suggestions",
                {
                    "term": term,
                    "cams": "",
                    "course_add": "CS",
                    "page_num": str(page),
                    "sco": "0",
                    "sio": "1",
                    "already": "",
                },
            )

            root = ElementTree.fromstring(document)
            results = root.findall(".//rs")

            for item in results:
                course = item.text.strip() if item.text else ""
                availability = item.attrib.get("info", "")

                is_unavailable = (
                    "not available in any term"
                    in availability.casefold()
                )

                if (
                    is_undergraduate_course(course)
                    and not is_unavailable
                    and (
                        "only" not in availability
                        or term_name in availability
                    )
                ):
                    codes.add(course)

            if len(results) < 20:
                return sorted(codes)

            page += 1

    def class_data(
        self,
        term: str,
        course: str,
    ) -> str:
        selected = self._post(
            "api/string-to-filter",
            {
                "term": term,
                "validations": "",
                "itemnames": course,
                "input": course,
                "reason": "CODE_NUMBER",
                "current": "",
                "isimport": "0",
                "strict": "0",
            },
        )

        if not selected:
            raise ValueError(
                f"VSB did not recognize {course}"
            )

        entry = selected[0]

        if entry.get("error"):
            raise ValueError(
                f"VSB rejected {course}: {entry['error']}"
            )

        parameters = {
            "term": term,
            "course_0_0": entry["cnKey"],
            "va_0_0": entry["va"],
            "rq_0_0": entry.get("reqId", ""),
            "nouser": "1",
            **cache_buster(),
        }

        return self._get(
            "api/class-data",
            parameters,
        )


def fetch_faculty_emails() -> dict[str, str]:
    opener = build_opener()
    emails: dict[str, str] = {}

    for page in range(0, 20):
        request = Request(
            f"{FACULTY_DIRECTORY_URL}?page={page}",
            headers={"User-Agent": "kent-cs-schedule-builder/1"},
        )
        with opener.open(request, timeout=30) as response:
            page_emails = parse_faculty_directory(response.read().decode("utf-8"))
        if not page_emails:
            break
        emails.update(page_emails)

    return emails


def load_overrides(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"email_overrides": {}, "extra_lectures": [], "room_overrides": {}, "skip_sections": []}
    with path.open(encoding="utf-8") as handle:
        overrides = json.load(handle)
    overrides.setdefault("email_overrides", {})
    overrides.setdefault("extra_lectures", [])
    overrides.setdefault("room_overrides", {})
    overrides.setdefault("skip_sections", [])
    return overrides


def import_semester(
    term: str,
    destination: Path,
    overrides_path: Path | None = None,
) -> dict[str, Any]:
    client = VsbClient()
    terms = client.terms()

    if term not in terms:
        raise ValueError(
            f"{term} is not available to VSB guest users"
        )

    faculty_emails = fetch_faculty_emails()
    overrides = load_overrides(
        overrides_path or destination.with_name(f"{term}.overrides.json")
    )
    room_overrides: dict[str, str] = overrides["room_overrides"]
    skip_sections: set[str] = set(overrides["skip_sections"])
    ambiguous_combined: list[str] = []

    lectures = [
        lecture
        for course in client.course_codes(term, terms[term])
        for lecture in parse_class_data(
            client.class_data(term, course),
            faculty_emails,
            room_overrides,
            ambiguous_combined,
            skip_sections,
        )
    ]

    email_overrides: dict[str, str] = overrides["email_overrides"]
    for lecture in lectures:
        override_email = email_overrides.get(lecture["instructor"] or "")
        if override_email:
            lecture["email"] = override_email

    lectures.extend(overrides["extra_lectures"])
    lectures.sort(key=lambda record: (record["course"], record["section"], record["crn"]))

    unresolved = sorted(
        {
            lecture["instructor"]
            for lecture in lectures
            if lecture["instructor"]
            and not lecture["email"]
        }
    )

    payload = {
        "version": 1,
        "term": {
            "id": term,
            "name": terms[term],
        },
        "imported_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(),
        ),
        "unresolved_instructors": unresolved,
        "ambiguous_combined_sections": sorted(set(ambiguous_combined)),
        "lectures": lectures,
    }

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=destination.parent,
        delete=False,
    ) as temporary:
        json.dump(
            payload,
            temporary,
            indent=2,
        )
        temporary.write("\n")
        temporary_path = Path(temporary.name)

    temporary_path.replace(destination)

    return payload
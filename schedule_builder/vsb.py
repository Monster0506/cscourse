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


def parse_class_data(
    document: str,
    faculty_emails: dict[str, str],
) -> list[dict[str, Any]]:
    root = ElementTree.fromstring(document)

    course = root.find(".//course")
    offering = root.find(".//offering")

    if course is None or offering is None:
        raise ValueError(
            "VSB class-data response has no course offering"
        )

    course_code = f"{course.attrib['code']} {course.attrib['number']}"
    title = offering.attrib.get("title", "")
    records: dict[str, dict[str, Any]] = {}

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

                crn = block.attrib["key"]
                instructor = (
                    block.attrib.get("teacher", "").strip() or None
                )

                normalized = (
                    normalize_instructor_name(instructor)
                    if instructor
                    else ""
                )

                record = records.setdefault(
                    crn,
                    {
                        "course": course_code,
                        "title": title,
                        "section": block.attrib.get("secNo", ""),
                        "crn": crn,
                        "instructor": instructor,
                        "email": (
                            faculty_emails.get(normalized)
                            if normalized
                            else None
                        ),
                        "campus": campus,
                        "component": block_type,
                        "room": block.attrib.get("location", ""),
                        "meetings": [],
                    },
                )

                for identifier in block.attrib.get(
                    "timeblockids",
                    "",
                ).split(","):
                    timeblock = timeblocks.get(identifier)

                    if timeblock is None:
                        continue

                    meeting = {
                        "day": DAY_NAMES.get(
                            timeblock.get("day", ""),
                            "Unknown",
                        ),
                        "start": clock_time(timeblock["t1"]),
                        "end": clock_time(timeblock["t2"]),
                    }

                    if meeting["day"] in {"Saturday", "Sunday"}:
                        continue

                    if meeting not in record["meetings"]:
                        record["meetings"].append(meeting)

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
    request = Request(
        FACULTY_DIRECTORY_URL,
        headers={
            "User-Agent": "kent-cs-schedule-builder/1",
        },
    )

    with build_opener().open(request, timeout=30) as response:
        return parse_faculty_directory(
            response.read().decode("utf-8")
        )


def import_semester(
    term: str,
    destination: Path,
) -> dict[str, Any]:
    client = VsbClient()
    terms = client.terms()

    if term not in terms:
        raise ValueError(
            f"{term} is not available to VSB guest users"
        )

    faculty_emails = fetch_faculty_emails()

    lectures = [
        lecture
        for course in client.course_codes(term, terms[term])
        for lecture in parse_class_data(
            client.class_data(term, course),
            faculty_emails,
        )
    ]

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
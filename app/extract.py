"""Turning free text into structured care items.

This is deliberately **not** a language model. It is a transparent, local,
rule-based reader: it finds medication changes, tasks, follow-ups and
observations in text a person typed or pasted, and proposes them as items for
a human to confirm. Nothing it produces reaches the record without someone
pressing "Add".

Keeping it rule-based is a feature, not a shortcut — every proposal can be
explained by pointing at the pattern that produced it, it works with no
network, and it cannot invent a medication dose. ``extract()`` returns
proposals plus the snippet that triggered each one.
"""

from __future__ import annotations

import re
from datetime import timedelta

from .timing import utcnow

MEDICATION_WORDS = (
    "mg", "mcg", "microgram", "milligram", "g", "ml", "iu", "units",
    "tablet", "tablets", "capsule", "capsules", "drops", "puff", "puffs",
)

# Verbs that mean "this dose changed".
CHANGE_VERBS = (
    "increased", "raised", "up to", "upped", "decreased", "reduced", "lowered",
    "down to", "changed to", "switched to", "stopped", "discontinued", "halved",
    "doubled", "started", "began", "prescribed", "added",
)

TASK_VERBS = (
    "call", "phone", "ring", "book", "rebook", "arrange", "order", "collect",
    "pick up", "drop off", "chase", "email", "check", "renew", "request",
    "schedule", "organise", "organize", "fill in", "send",
)

OBSERVATION_WORDS = (
    "tired", "exhausted", "dizzy", "confused", "forgetful", "unsteady", "fell",
    "pain", "ache", "sore", "bruise", "appetite", "ate", "eating", "drank",
    "sleep", "slept", "mood", "anxious", "low", "cheerful", "breathless",
    "swelling", "swollen", "temperature", "shaky", "nauseous", "constipated",
)

APPOINTMENT_WORDS = (
    "appointment", "follow-up", "follow up", "review", "clinic", "scan",
    "x-ray", "blood test", "bloods", "test", "consultant", "surgery",
)

COMMUNICATION_WORDS = ("called", "phoned", "spoke to", "emailed", "asked", "told")

DOSE_PATTERN = re.compile(
    r"\b(\d+(?:[.,]\d+)?)\s*(mg|mcg|microgram|milligram|g|ml|iu|units?|tablets?|capsules?|drops?|puffs?)\b",
    re.IGNORECASE,
)

MED_NAME = re.compile(r"\b([A-Z][A-Za-z\-]{2,25})\b")

WEEK_WORDS = {
    "one": 1, "a": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12,
}
MONTH_WORDS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Known care-vocabulary nouns we should not mistake for a person's name or med.
STOPWORDS = {
    "The", "This", "That", "They", "She", "He", "His", "Her", "And", "But",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
    "Dr", "Doctor", "Nurse", "Pharmacy", "Hospital", "Clinic", "GP", "Blood",
    "Please", "Today", "Tomorrow", "Next", "Evening", "Morning", "Night",
}

# Words that sit between a drug name and its dose. Walking backwards past these
# is what lets "doctor increased ramipril to 5mg" find "ramipril" even though
# the note was typed in lower case.
FILLERS = {
    "to", "from", "up", "down", "at", "the", "a", "an", "of", "per", "for",
    "daily", "twice", "once", "nightly", "now", "is", "was", "be", "been",
    "and", "then", "again", "his", "her", "their", "she", "he", "they",
    "doctor", "nurse", "consultant", "dose", "doses", "dosage", "tablet",
    "tablets", "capsule", "capsules", "md", "rx", "prescription",
}

VERB_WORDS = {
    word for verb in CHANGE_VERBS for word in verb.split() if len(word) > 2
}


def _sentences(text: str) -> list[str]:
    cleaned = (text or "").replace("\r", "\n")
    parts = re.split(r"(?<=[.!?;])\s+|\n+", cleaned)
    return [part.strip(" \t-•*") for part in parts if part.strip(" \t-•*")]


def _looks_like_dose(text: str) -> bool:
    return bool(DOSE_PATTERN.search(text))


def _medication_name(sentence: str, dose_match) -> str:
    """The drug name nearest before the dose, capitalised or not.

    Walking backwards past filler words ("to", "the", "daily") and past the
    change verb itself is what makes a lower-case note readable:
    "doctor increased ramipril to 5mg" -> "Ramipril".
    """
    before = sentence[: dose_match.start()]
    words = re.findall(r"[A-Za-z][A-Za-z\-]{2,25}", before)
    for word in reversed(words):
        lowered = word.lower()
        if lowered in FILLERS or lowered in VERB_WORDS:
            continue
        if word in STOPWORDS:
            continue
        return word[:1].upper() + word[1:]
    return ""


def _relative_due(text: str):
    """'in 2 weeks', 'tomorrow', '12 October' -> a date, or None."""
    lowered = text.lower()
    now = utcnow()

    if "tomorrow" in lowered:
        return now + timedelta(days=1)
    if "next week" in lowered:
        return now + timedelta(days=7)

    match = re.search(r"in\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve)\s+(day|days|week|weeks|month|months)", lowered)
    if match:
        raw, unit = match.groups()
        amount = int(raw) if raw.isdigit() else WEEK_WORDS.get(raw, 0)
        if unit.startswith("day"):
            return now + timedelta(days=amount)
        if unit.startswith("week"):
            return now + timedelta(weeks=amount)
        return now + timedelta(days=30 * amount)

    match = re.search(r"\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b", lowered)
    if match:
        day, month = int(match.group(1)), MONTH_WORDS.get(match.group(2))
        if month:
            year = now.year if month >= now.month else now.year + 1
            try:
                return now.replace(
                    year=year, month=month, day=day, hour=9, minute=0, second=0, microsecond=0
                )
            except ValueError:
                return None
    return None


def _due_date_only(value):
    return value.date().isoformat() if value else None


def extract(text: str, *, today=None) -> list[dict]:
    """Return proposed items. Empty list means nothing recognisable was found."""
    proposals: list[dict] = []
    seen: set[tuple] = set()

    def add(kind: str, **fields):
        signature = (kind, fields.get("text") or fields.get("title") or fields.get("name"))
        if signature in seen:
            return
        seen.add(signature)
        proposals.append({"type": kind, "source": fields.pop("source", ""), **fields})

    for sentence in _sentences(text):
        lowered = sentence.lower()
        dose = DOSE_PATTERN.search(sentence)
        matched = False

        # --- medication change -------------------------------------------------
        if dose and any(verb in lowered for verb in CHANGE_VERBS):
            name = _medication_name(sentence, dose)
            amount = f"{dose.group(1).replace(',', '.')} {dose.group(2).lower()}"
            stopped = any(word in lowered for word in ("stopped", "discontinued"))
            if name:
                add(
                    "medication",
                    name=name,
                    strength="" if stopped else amount,
                    action="stop" if stopped else "change",
                    detail=sentence,
                    source=sentence,
                    text=sentence,
                )
                matched = True

        # --- follow-up appointment --------------------------------------------
        # Evaluated independently: one sentence can carry both a dose change and
        # a request for a test, and the person confirming can drop either.
        if any(word in lowered for word in APPOINTMENT_WORDS) and (
            "within" in lowered
            or "in " in lowered
            or "book" in lowered
            or "recommend" in lowered
            or "request" in lowered
            or "wants" in lowered
        ):
            due = _relative_due(sentence)
            add(
                "appointment",
                title=sentence[:120],
                due_at=_due_date_only(due),
                source=sentence,
                text=sentence,
            )
            matched = True

        # --- explicit task -----------------------------------------------------
        if not matched and any(
            f"{verb} " in lowered or lowered.startswith(verb) for verb in TASK_VERBS
        ):
            due = _relative_due(sentence)
            add(
                "task",
                title=sentence[:160],
                due_at=_due_date_only(due),
                source=sentence,
                text=sentence,
            )
            matched = True

        # --- communication -----------------------------------------------------
        if any(word in lowered for word in COMMUNICATION_WORDS):
            add("communication", text=sentence, source=sentence)
            continue

        # --- observation -------------------------------------------------------
        if not matched and any(word in lowered for word in OBSERVATION_WORDS):
            add("observation", text=sentence, source=sentence)

    return proposals


def summarise(proposals: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for proposal in proposals:
        counts[proposal["type"]] = counts.get(proposal["type"], 0) + 1
    return counts

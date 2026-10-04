"""Conservative English triage hints, not a complete emergency detection system."""

import re
from dataclasses import dataclass

from condo_classifier.schema import Category, Priority


@dataclass(frozen=True)
class SafetySignal:
    category: Category
    evidence: str


EMERGENCY_PATTERNS = (
    (Category.MAINTENANCE, r"\b(?:on fire|flames|smoke (?:is |coming |pouring |in |from ))\b"),
    (Category.MAINTENANCE, r"\b(?:gas leak|smell (?:of )?gas|smelling gas)\b"),
    (
        Category.MAINTENANCE,
        r"\b(?:trapped|stuck) (?:inside |in )(?:the |a |our )?(?:lift|elevator)\b",
    ),
    (Category.MAINTENANCE, r"\b(?:sparks|sparking|exposed live wire|electrocut\w*)\b"),
    (Category.SECURITY, r"\b(?:attacking|assaulting|unconscious|not breathing)\b"),
)
HYPOTHETICAL = re.compile(
    r"\b(?:what (?:should|would|do)|in case of|if there|if someone|fire drill|"
    r"fire safety|evacuation plan|training exercise|yesterday|last week|resolved)\b",
    re.I,
)
NEGATION = re.compile(r"\b(?:no|not|never|without|isn't|isnt|wasn't|wasnt)\b", re.I)
HIGH_PRIORITY = re.compile(
    r"\b(?:flooding|burst pipe|no water|power outage|no power|break[ -]?in|break into|"
    r"unregistered stranger|suspicious|blocking|obstructing|lift.{0,25}stuck)\b",
    re.I,
)
SENSITIVE = re.compile(
    r"\b(?:cctv|footage|video|refund\w*|waiv\w*|penalty|legal|liability|"
    r"deactivat\w*|stolen|approv\w*|contractor|deposit)\b",
    re.I,
)
INSTRUCTION_ATTACK = re.compile(
    r"\b(?:ignore (?:all |your |the |previous )*(?:rules|instructions)|system prompt|"
    r"api key|classify (?:this|it|everything) as|output (?:only|an approved))\b",
    re.I,
)
TOPIC_CUES = {
    Category.MAINTENANCE: r"\b(?:leak\w*|broken|flicker\w*|plumber|repair)\b",
    Category.SECURITY: r"\b(?:access card|intruder|stolen|cctv)\b",
    Category.NOISE: r"\b(?:loud music|noise|noisy|barking|stomping)\b",
    Category.CLEANLINESS: r"\b(?:rubbish|cockroach\w*|rat|rats|litter|dirty)\b",
    Category.FACILITIES: r"\b(?:book|reserve|reservation)\b.{0,30}\b(?:court|room|pit|clubhouse)\b",
    Category.PARKING: r"\b(?:parking|parked|parking bay)\b",
    Category.BILLING: r"\b(?:invoice|receipt|charged twice|refund|payment)\b",
    Category.RENOVATION: r"\b(?:renovation|moving out|moving in|movers)\b",
}


def safety_signal(text: str) -> SafetySignal | None:
    # Scope negation to a short prefix and each clause, not the entire message.
    for clause in re.split(r"[.!?;\n]|\bbut\b|\bhowever\b", text, flags=re.I):
        if HYPOTHETICAL.search(clause):
            continue
        for category, pattern in EMERGENCY_PATTERNS:
            for match in re.finditer(pattern, clause, re.I):
                prefix = clause[max(0, match.start() - 28) : match.start()]
                if not NEGATION.search(prefix):
                    return SafetySignal(category, match.group().strip())
    return None


def priority_hint(text: str) -> Priority:
    return Priority.HIGH if HIGH_PRIORITY.search(text) else Priority.NORMAL


def location_hint(text: str) -> str | None:
    # Grounded spans only. A unit, block, numbered bay, floor or named shared area.
    pattern = (
        r"#\d{1,3}-\d{1,4}\b|\b(?:block|blk)\s+[a-z0-9]+\b|"
        r"\b(?:level|floor)\s+\d+\b|\b(?:lot|bay)\s+[a-z]?\d+\b|"
        r"\b(?:guardhouse|bin centre|playground|carpark|gym|lobby|"
        r"swimming pool|tennis court|common toilet|common washroom)\b"
    )
    found = re.search(pattern, text, re.I)
    return found.group() if found else None


def multiple_topics(text: str) -> bool:
    # Only independent clauses with distinct domain cues. This is a review hint.
    clauses = re.split(r"[.!?;\n]|\band also\b|\balso\b|\band\b", text, flags=re.I)
    topics: set[Category] = set()
    matching_clauses = 0
    for clause in clauses:
        matches = {key for key, pattern in TOPIC_CUES.items() if re.search(pattern, clause, re.I)}
        if matches:
            matching_clauses += 1
            topics.update(matches)
    return matching_clauses > 1 and len(topics) > 1

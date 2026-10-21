"""Conservative triage hints, not a complete emergency detection system.

The rules are written for English. Messages in other languages are translated first; the
Chinese, Malay and Tamil phrases below are a backstop for when translation is missing or wrong.
"""

import re
from dataclasses import dataclass

from condo_classifier.schema import Category, Priority


@dataclass(frozen=True)
class SafetySignal:
    category: Category
    evidence: str


EMERGENCY_PATTERNS = (
    (Category.MAINTENANCE, r"\b(?:on fire|flames|smoke (?:is |coming |pouring |in |from ))\b"),
    # Everyday phrasings: "there's a fire", "fire in the lift lobby", "caught fire".
    (
        Category.MAINTENANCE,
        r"\b(?:there(?:'s| is| are)|i (?:can )?see) (?:a |some )?(?:fire|smoke|flames)\b",
    ),
    (Category.MAINTENANCE, r"\bfire (?:in|at|on|inside|near|spreading)\b"),
    (Category.MAINTENANCE, r"\b(?:caught|catching|catch) fire\b|\b(?:is|are) (?:burning|ablaze)\b"),
    (
        Category.MAINTENANCE,
        r"\b(?:full of|filled with) smoke\b|\bsmoke everywhere\b|"
        r"\bburning smell\b|\bsmell(?:s|ing)? (?:of )?burning\b",
    ),
    (Category.MAINTENANCE, r"\b(?:gas leak|smell (?:of )?gas|smelling gas)\b"),
    (
        Category.MAINTENANCE,
        r"\b(?:trapped|stuck) (?:inside |in )(?:the |a |our )?(?:lift|elevator)\b",
    ),
    # The lift named first: "the lift is stuck and someone is inside", "elevator stuck with
    # my son in it". A stuck lift with nobody in it stays a normal repair.
    (
        Category.MAINTENANCE,
        r"\b(?:lift|elevator)s?\b.{0,40}?\b(?:stuck|stopped|jammed|broke down|broken down)\b"
        r".{0,40}?\b(?:someone|somebody|people|persons?|passengers?|residents?|kids?|"
        r"child(?:ren)?|son|daughter|baby|mum|mom|mother|dad|father|grandma|grandpa|elderly|"
        r"i'm|i am|we're|we are)\b",
    ),
    (
        Category.MAINTENANCE,
        r"\b(?:someone|somebody|people|kids?|child(?:ren)?|son|daughter|baby|mum|mom|mother|"
        r"dad|father|grandma|grandpa|i'm|i am|we're|we are)\b.{0,30}?\b(?:in|inside) "
        r"(?:the |a |our )?(?:lift|elevator)\b.{0,30}?\b(?:stuck|stopped|jammed)\b",
    ),
    (Category.MAINTENANCE, r"\b(?:sparks|sparking|exposed live wire|electrocut\w*)\b"),
    (Category.SECURITY, r"\b(?:attacking|assaulting|unconscious|not breathing)\b"),
    # Injuries and medical emergencies: a person who is hurt right now.
    (
        Category.SECURITY,
        # A person falling, not paint or leaves: a person word shortly before the verb.
        r"\b(?:i|he|she|they|someone|somebody|person|man|woman|lady|boy|girl|child|kid|"
        r"baby|son|daughter|wife|husband|mum|mom|mother|dad|father|grandma|grandpa|"
        r"grandmother|grandfather|granny|aunt|uncle|neighbou?r|resident|elderly|helper|"
        r"maid|worker|cleaner|guard)\b[^.!?\n]{0,25}?\b(?:fell|has fallen|have fallen|"
        r"tripped|slipped) (?:down|off|from|over|on)\b|"
        r"\b(?:injured|bleeding|collapsed|fainted|passed out|seizure|choking|"
        r"heart attack|stroke|can'?t get up|can'?t breathe|cannot breathe|"
        r"broke (?:his|her|my|their) (?:leg|arm|hip|neck|back)|hit (?:his|her|their) head)\b",
    ),
)
# Clear present-danger phrases only. Malay and Tamil wording needs native-speaker review.
OTHER_LANGUAGE_EMERGENCY_PATTERNS = (
    # Chinese (simplified and traditional)
    (Category.MAINTENANCE, r"着火|著火|起火|失火|火灾|火災|冒烟|冒煙|浓烟|濃煙"),
    (Category.MAINTENANCE, r"煤气泄漏|煤氣洩漏|漏气|漏氣|闻到煤气|聞到煤氣|燃气泄漏"),
    (Category.MAINTENANCE, r"(?:困|关|關)在电梯|(?:困|关|關)在電梯|电梯里被困|電梯裡被困"),
    (Category.MAINTENANCE, r"触电|觸電|冒火花"),
    (Category.SECURITY, r"昏迷|不省人事|没有呼吸|沒有呼吸|被攻击|被攻擊"),
    # Malay
    (Category.MAINTENANCE, r"\b(?:kebakaran|terbakar|asap tebal)\b"),
    (Category.MAINTENANCE, r"\b(?:bocor gas|kebocoran gas|bau gas)\b"),
    (Category.MAINTENANCE, r"\b(?:terperangkap|tersekat) (?:dalam |di dalam )?lif\b"),
    (Category.MAINTENANCE, r"\b(?:renjatan elektrik|kejutan elektrik)\b"),
    (Category.SECURITY, r"\b(?:pengsan|tidak sedarkan diri|tak sedarkan diri|tidak bernafas)\b"),
    # Tamil
    (Category.MAINTENANCE, r"தீப்பிடி|தீ விபத்து|புகை வருகிறது"),
    (Category.MAINTENANCE, r"எரிவாயு கசிவு|கேஸ் கசிவு"),
    (Category.MAINTENANCE, r"லிஃப்டில் சிக்கி|லிப்டில் சிக்கி|மின்தூக்கியில் சிக்கி"),
    (Category.MAINTENANCE, r"மின்சாரம் தாக்கி"),
    (Category.SECURITY, r"மயக்கம்|மூச்சு விடவில்லை"),
)
# "No fire" style negation right before the phrase. Tamil negates after the verb, so it has none.
OTHER_LANGUAGE_NEGATION = re.compile(
    r"(?:没有|沒有|没|沒|不是|无|無)$|\b(?:tidak|tiada|bukan|tak)\s+$", re.I
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
    for clause in re.split(r"[.!?;\n。！？；]|\bbut\b|\bhowever\b", text, flags=re.I):
        if not HYPOTHETICAL.search(clause):
            for category, pattern in EMERGENCY_PATTERNS:
                for match in re.finditer(pattern, clause, re.I):
                    prefix = clause[max(0, match.start() - 28) : match.start()]
                    if not NEGATION.search(prefix):
                        return SafetySignal(category, match.group().strip())
        # No hypothetical filter here: over-escalating beats missing an emergency.
        for category, pattern in OTHER_LANGUAGE_EMERGENCY_PATTERNS:
            for match in re.finditer(pattern, clause, re.I):
                prefix = clause[max(0, match.start() - 12) : match.start()]
                if not OTHER_LANGUAGE_NEGATION.search(prefix):
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

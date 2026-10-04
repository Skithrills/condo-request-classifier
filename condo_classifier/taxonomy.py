from dataclasses import dataclass

from condo_classifier.schema import Category


@dataclass(frozen=True)
class Label:
    name: str
    description: str
    team: str
    example: str
    needs_location: bool = False


LABELS: dict[Category, Label] = {
    Category.MAINTENANCE: Label(
        "Maintenance",
        "Physical faults: leaks, lifts, lighting, plumbing and damaged equipment.",
        "Maintenance team",
        "The corridor light outside #05-12 keeps flickering.",
        True,
    ),
    Category.SECURITY: Label(
        "Security & access",
        "Suspicious activity, lost access cards, entry permissions and CCTV.",
        "Security desk",
        "I lost my access card and need a replacement.",
        True,
    ),
    Category.NOISE: Label(
        "Noise & nuisance",
        "Noise, disruptive behaviour and neighbour disturbance complaints.",
        "Resident relations",
        "Loud music from #08-21 has continued past midnight.",
        True,
    ),
    Category.CLEANLINESS: Label(
        "Cleaning & pests",
        "Rubbish, dirty common areas, pests and housekeeping requests.",
        "Estate services",
        "There are cockroaches near the Block B rubbish chute.",
        True,
    ),
    Category.FACILITIES: Label(
        "Facilities & bookings",
        "Bookings, availability and use of shared facilities. Faults go to maintenance.",
        "Facilities desk",
        "Can I book the function room for Saturday afternoon?",
    ),
    Category.PARKING: Label(
        "Parking",
        "Parking allocation, permits, visitor vehicles and obstructed parking spaces.",
        "Parking desk",
        "A car is parked in my allocated lot B123.",
        True,
    ),
    Category.BILLING: Label(
        "Billing & payments",
        "Maintenance fees, invoices, receipts, deposits and refund enquiries.",
        "Finance team",
        "My maintenance fee was charged twice this month.",
    ),
    Category.RENOVATION: Label(
        "Renovation & moving",
        "Renovation applications, contractor access and move-in or move-out arrangements.",
        "Estate management",
        "What documents do I need before starting renovation?",
    ),
    Category.GENERAL: Label(
        "General enquiries",
        "Office contacts, by-laws, notices, policy questions and general estate information.",
        "Management office",
        "What time does the management office open on Sundays?",
    ),
    Category.OTHER: Label(
        "Other / unclear",
        "Unclear requests, unrelated topics, unsolicited offers or insufficient context.",
        "Management office",
        "Hello, can someone help me with something?",
    ),
}


def taxonomy_prompt() -> str:
    return "\n".join(f"- {key.value}: {label.description}" for key, label in LABELS.items())

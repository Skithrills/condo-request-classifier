"""Resident-facing reply text. Kept free of Telegram imports so any chat channel can reuse it."""

from condo_classifier.schema import Classification, Priority
from condo_classifier.taxonomy import LABELS

WELCOME = (
    "Hi! I'm the management office assistant.\n\n"
    "Tell me what you need, including your block, unit or the shared area involved. "
    'For example: "The corridor light outside #05-12 keeps flickering."\n\n'
    "I sort requests for the right team. I can't approve, book or pay for anything, "
    "and staff confirm every request.\n\n"
    "Send /new to start a separate request.\n\n"
    "If anyone is in danger, call your local emergency number first."
)

NEW_REQUEST = "Okay, starting a new request. What do you need help with?"

TEXT_ONLY = "Please describe your request in a text message. I can't read photos or files yet."

INVALID = "Please describe your request in words, between 3 and 4,000 characters."

LIMIT_REACHED = (
    "You've reached today's message limit, so I can't sort more requests until tomorrow. "
    "Please contact the management office directly if it can't wait.\n\n"
    "If anyone is in danger, call your local emergency number first."
)

UNAVAILABLE = (
    "Sorry, I couldn't sort your request right now. Please try again shortly or "
    "contact the management office directly."
)


def resident_reply(result: Classification) -> str:
    """Summarise a classification for the resident without internal scores or review notes."""
    if result.status == "safety_escalation":
        return (
            "This sounds urgent. If anyone is in danger, call your local emergency number "
            "now and move to a safe place.\n\n"
            "Your message has been marked as an emergency for building staff, but this chat "
            "is not an emergency service."
        )
    if result.status == "provider_error":
        return UNAVAILABLE
    label = LABELS[result.category]
    lines = [f"Thanks, your request has been sorted as: {label.name}."]
    lines.append(f"Suggested team: {result.suggested_team}.")
    if result.location:
        lines.append(f"Location: {result.location}.")
    if result.priority == Priority.HIGH:
        lines.append("It has been marked as high priority.")
    if result.missing_details:
        lines.append("")
        lines.append("To help staff act faster, please reply with:")
        lines.extend(f"- {detail}" for detail in result.missing_details)
    lines.append("")
    if result.review_required:
        lines.append("A staff member will check it before anything is arranged.")
    else:
        lines.append("Staff will follow up. Nothing has been approved or booked yet.")
    return "\n".join(lines)

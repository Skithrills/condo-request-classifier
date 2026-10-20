"""Resident-facing reply text. Kept free of Telegram imports so any chat channel can reuse it."""

import re

from condo_classifier.schema import Category, Classification, Priority
from condo_classifier.taxonomy import LABELS

WELCOME = (
    "Hi! I'm the management office assistant.\n\n"
    "Tell me what you need, including your block, unit or the shared area involved. "
    'For example: "The corridor light outside #05-12 keeps flickering."\n\n'
    "I sort requests for the right team. I can't approve, book or pay for anything, "
    "and staff confirm every request.\n\n"
    "Send /new to start a separate request, or /human to reach a staff member.\n\n"
    "If anyone is in danger, call your local emergency number first."
)

NEW_REQUEST = "Okay, starting a new request. What do you need help with?"

TEXT_ONLY = "Please send your request as text or a photo. I can't read other files yet."

PHOTO_UNAVAILABLE = "I can't look at photos right now. Please describe the problem in text."

PHOTO_UNREADABLE = "I couldn't make out that photo. Could you describe the problem in text?"

PHOTO_TOO_BIG = "That file is too big for me. Please send it as a normal photo instead."

INVALID = "Please describe your request in words, between 3 and 4,000 characters."

UNAVAILABLE = (
    "Sorry, I couldn't sort your request right now. Please try again shortly or "
    "contact the management office directly."
)


EMERGENCY = (
    "🚨 If anyone is in danger, call your local emergency number now. I've alerted "
    "building staff, but this chat isn't an emergency service."
)


def resident_reply(result: Classification) -> str:
    """Short template reply without internal scores or review notes. Used when Claude is off
    or unavailable, and always for emergencies."""
    if result.status == "safety_escalation":
        return EMERGENCY
    if result.status == "provider_error":
        return UNAVAILABLE
    if result.category == Category.OTHER:
        return "Can you tell me a bit more about what you need?"
    reply = f"Got it, this one's for {result.suggested_team}."
    if result.priority == Priority.HIGH:
        reply += " Marked high priority."
    if result.missing_details:
        detail = result.missing_details[0]
        reply += f" Can you share the {detail[0].lower()}{detail[1:]}?"
    else:
        reply += " Tap Submit below to send it to staff."
    return reply


SUBMITTED = "✅ Submitted as {request_id}. Staff will follow up. Send /new for anything else."

CANCELLED = "Cancelled. Message me anytime if you need something."

HUMAN = (
    "🙋 I've asked a staff member to take over {request_id}. They'll reply here. "
    "You can keep adding details in the meantime."
)

# Staff names are not shown to residents.
STAFF_MESSAGE = "👤 Staff: {text}"

HANDED_BACK = (
    "✔️ The staff member has finished with {request_id}. I'm back. "
    "Message me anytime if you need anything else."
)

STAFF_UNKNOWN = "I can't tell which request that is. Reply to the request card."

STAFF_TEXT_ONLY = "Only text replies are sent to residents for now."

STAFF_UNDELIVERED = "⚠️ Couldn't deliver that to the resident (they may have blocked the bot)."

STALE = "This request is already closed."

NOT_FOR_YOU = "These buttons are for the person who made this request."

HUMAN_FIRST = "Tell me briefly what it's about first, then send /human and I'll get a staff member."

HUMAN_ALREADY = "A staff member has already been asked to take over. They'll contact you."

CLOSING = "Anytime! 👋 Message me whenever you need something."

ACKNOWLEDGED = "👍 Tell me whenever you have more details."

GREETING_REPLY = "Hi! 👋 What can I help with? Include your block or unit if it's about a place."

GREETING = re.compile(
    r"^\W*(?:hi+|hey+|hello+|hiya|yo|good (?:morning|afternoon|evening))(?: there)?\W*$", re.I
)

_PLEASANTRY_WORD = (
    r"(?:ok(?:ay)?|k|cool|great|nice|noted|alright|got it|perfect|awesome|"
    r"thanks?(?: you)?(?: so much| a lot)?|thx|ty|cheers|bye|goodbye|see ya)"
)
# One or more pleasantries, e.g. "ok thank you", "great, thanks!", "thx bye 👍".
PLEASANTRY = re.compile(rf"^\W*{_PLEASANTRY_WORD}(?:[\W_]+{_PLEASANTRY_WORD})*\W*$", re.I)

NOT_READY = "Add the missing details first."


def work_order_summary(result: Classification) -> str:
    """Compact work-order card shown above the Submit and Cancel buttons."""
    parts = [LABELS[result.category].name]
    if result.location:
        parts.append(result.location)
    lines = ["📋 " + " · ".join(parts), f"Team: {result.suggested_team}"]
    if result.priority == Priority.HIGH:
        lines.append("Priority: high")
    return "\n".join(lines)


def quick_reply(text: str, fresh: bool) -> str | None:
    """Canned answer for small talk that shouldn't start or change a request.

    `fresh` means no request is in progress, so "thanks" is a goodbye, not a detail.
    """
    if GREETING.match(text):
        return GREETING_REPLY
    if PLEASANTRY.match(text):
        if fresh:
            return CLOSING
        # "ok" or "ty" mid-request is too short to classify; don't call it invalid.
        if len(text.strip()) < 3:
            return ACKNOWLEDGED
    return None

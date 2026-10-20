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

TEXT_OR_VOICE = (
    "Please send your request as text, a voice message or a photo. I can't read other files yet."
)

VOICE_TOO_LONG = "Please keep voice messages under {seconds} seconds, or type your request."

VOICE_FAILED = (
    "Sorry, I couldn't make out that voice message. Please try again or type your request."
)

HEARD = "🎙️ I heard: {text}"

HEARD_HINT = "\n\nWrong language? Send /language to choose yours."

CHOOSE_LANGUAGE = "Which language will you speak in voice messages?"

LANGUAGE_SET = "Got it. Voice messages: {language}. Send /language to change."

INVALID = "Please describe your request in words, between 3 and 4,000 characters."

UNAVAILABLE = (
    "Sorry, I couldn't sort your request right now. Please try again shortly or "
    "contact the management office directly."
)


EMERGENCY = (
    "🚨 If anyone is in danger, call your local emergency number now. I've alerted "
    "building staff, but this chat isn't an emergency service."
)


# Fixed translations, shown above the English. Malay and Tamil need native-speaker review.
EMERGENCY_TRANSLATIONS = {
    "zh": (
        "这听起来很紧急。如果有人有危险，请立即拨打当地的紧急电话，并转移到安全的地方。\n\n"
        "您的信息已作为紧急情况通知大楼工作人员，但这个聊天不是紧急服务。"
    ),
    "ms": (
        "Ini kedengaran mendesak. Jika sesiapa dalam bahaya, hubungi nombor kecemasan "
        "tempatan sekarang dan pergi ke tempat yang selamat.\n\n"
        "Mesej anda telah ditandakan sebagai kecemasan untuk kakitangan bangunan, tetapi "
        "sembang ini bukan perkhidmatan kecemasan."
    ),
    "ta": (
        "இது அவசரமாகத் தெரிகிறது. யாருக்காவது ஆபத்து இருந்தால், உடனே உங்கள் உள்ளூர் அவசர "
        "எண்ணை அழைத்து பாதுகாப்பான இடத்திற்குச் செல்லுங்கள்.\n\n"
        "உங்கள் செய்தி கட்டிடப் பணியாளர்களுக்கு அவசரநிலையாகக் குறிக்கப்பட்டுள்ளது, ஆனால் "
        "இந்த அரட்டை அவசர சேவை அல்ல."
    ),
}
EMERGENCY_TRANSLATIONS["id"] = EMERGENCY_TRANSLATIONS["ms"]  # Close enough to be understood.


def emergency_reply(language: str | None = None) -> str:
    translated = EMERGENCY_TRANSLATIONS.get(language or "")
    return f"{translated}\n\n{EMERGENCY}" if translated else EMERGENCY


def resident_reply(result: Classification, language: str | None = None) -> str:
    """Short template reply without internal scores or review notes. Used when Claude is off
    or unavailable, and always for emergencies."""
    if result.status == "safety_escalation":
        return emergency_reply(language)
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

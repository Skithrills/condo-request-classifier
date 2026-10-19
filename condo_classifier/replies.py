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

TEXT_OR_VOICE = (
    "Please describe your request in a text or voice message. I can't read photos or files yet."
)

VOICE_TOO_LONG = "Please keep voice messages under {seconds} seconds, or type your request."

VOICE_FAILED = (
    "Sorry, I couldn't make out that voice message. Please try again or type your request."
)

HEARD = "🎙️ I heard: {text}"

INVALID = "Please describe your request in words, between 3 and 4,000 characters."

UNAVAILABLE = (
    "Sorry, I couldn't sort your request right now. Please try again shortly or "
    "contact the management office directly."
)


EMERGENCY = (
    "This sounds urgent. If anyone is in danger, call your local emergency number "
    "now and move to a safe place.\n\n"
    "Your message has been marked as an emergency for building staff, but this chat "
    "is not an emergency service."
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
    """Summarise a classification for the resident without internal scores or review notes."""
    if result.status == "safety_escalation":
        return emergency_reply(language)
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

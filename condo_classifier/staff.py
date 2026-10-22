"""Staff-facing request cards. Kept free of Telegram imports so other channels can reuse them."""

import secrets
from typing import TYPE_CHECKING

from condo_classifier.language import language_name
from condo_classifier.schema import Classification, Priority
from condo_classifier.taxonomy import LABELS

if TYPE_CHECKING:
    from condo_classifier.chat import ResidentMessage

MAX_CARD_CHARS = 4000  # Telegram allows 4,096

HEADERS = {
    "safety_escalation": "🚨 EMERGENCY",
    "provider_error": "⚠️ Could not classify",
}


def new_request_id() -> str:
    return "R-" + secrets.token_hex(3).upper()


def staff_card(
    request_id: str,
    resident: str,
    result: Classification,
    said: list["ResidentMessage"],
    *,
    language: str | None = None,
    translation_failed: bool = False,
) -> str:
    """Summarise a resident request for the staff group. Scores stay out; reasons stay in."""
    if result.status in HEADERS:
        header = HEADERS[result.status]
    elif result.priority == Priority.HIGH:
        header = "🔴 High priority"
    else:
        header = "🆕 Request"
    lines = [
        f"{header} · {request_id}",
        f"From: {resident}",
        "",
        f"Category: {LABELS[result.category].name}",
        f"Team: {result.suggested_team}",
        f"Priority: {result.priority}",
        f"Location: {result.location or 'not given'}",
    ]
    if language:
        lines.append(f"Language: {language_name(language)} (translated to English below)")
    if translation_failed:
        lines.append("⚠️ Some messages could not be translated; read them as written.")
    if result.missing_details:
        lines.append("Still missing: " + "; ".join(result.missing_details))
    if result.review_reasons:
        lines.append("")
        lines.append("Review notes:")
        lines.extend(f"- {reason}" for reason in result.review_reasons)
    lines.append("")
    lines.append("Resident said:")
    head = "\n".join(lines)
    quoted = "\n".join(quote(message) for message in said)
    room = MAX_CARD_CHARS - len(head) - 1
    if len(quoted) > room:
        # Keep the newest messages; earlier ones are already in previous card versions.
        quoted = "…" + quoted[-(room - 1) :]
    return f"{head}\n{quoted}"


def work_order_title(request_id: str, result: Classification) -> str:
    """Short card name so staff can scan the board: ID, category and where."""
    prefix = "🚨 " if result.status == "safety_escalation" else ""
    location = result.location or "location not given"
    return f"{prefix}{request_id} · {LABELS[result.category].name} · {location}"


def quote(message: "ResidentMessage") -> str:
    line = f"> {'🎙️ ' if message.voice else ''}{message.text}"
    if message.english != message.text:
        line += f"\n  EN: {message.english}"
    return line


def resident_label(full_name: str, username: str | None, user_id: int) -> str:
    handle = f" @{username}" if username else ""
    return f"{full_name}{handle} (Telegram ID {user_id})"

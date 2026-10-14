"""Staff-facing request cards. Kept free of Telegram imports so other channels can reuse them."""

import secrets

from condo_classifier.schema import Classification, Priority
from condo_classifier.taxonomy import LABELS

MAX_CARD_CHARS = 4000  # Telegram allows 4,096

# Request statuses (see store.STATUSES) that override the classification-based header.
STATUS_HEADERS = {
    "emergency": "🚨 EMERGENCY",
    "needs_human": "🙋 Resident wants a person",
    "submitted": "✅ Work order submitted",
    "cancelled": "✖️ Cancelled by resident",
    "ready": "📝 Ready, waiting for resident",
}


def new_request_id() -> str:
    return "R-" + secrets.token_hex(3).upper()


def staff_card(
    request_id: str,
    resident: str,
    result: Classification,
    resident_texts: list[str],
    status: str = "open",
) -> str:
    """Summarise a resident request for the staff group. Scores stay out; reasons stay in."""
    if result.status == "safety_escalation":
        header = STATUS_HEADERS["emergency"]
    elif status in STATUS_HEADERS:
        header = STATUS_HEADERS[status]
    elif result.status == "provider_error":
        header = "⚠️ Could not classify"
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
    if result.missing_details:
        lines.append("Still missing: " + "; ".join(result.missing_details))
    if result.review_reasons:
        lines.append("")
        lines.append("Review notes:")
        lines.extend(f"- {reason}" for reason in result.review_reasons)
    lines.append("")
    lines.append("Resident said:")
    head = "\n".join(lines)
    quoted = "\n".join(f"> {text}" for text in resident_texts)
    room = MAX_CARD_CHARS - len(head) - 1
    if len(quoted) > room:
        # Keep the newest messages; earlier ones are already in previous card versions.
        quoted = "…" + quoted[-(room - 1) :]
    return f"{head}\n{quoted}"


def resident_label(full_name: str, username: str | None, user_id: int) -> str:
    handle = f" @{username}" if username else ""
    return f"{full_name}{handle} (Telegram ID {user_id})"

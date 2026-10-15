"""Staff dashboard for requests that residents send through the Telegram bot.

Run with run_dashboard.cmd (Windows), ./run_dashboard.sh or `uv run streamlit run dashboard.py`.
It reads the same SQLite file the bot writes, so run both on the same machine.
"""

from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from condo_classifier.schema import Category
from condo_classifier.store import RequestStore, default_store_path
from condo_classifier.taxonomy import LABELS

load_dotenv(Path(__file__).resolve().parent / ".env")
st.set_page_config(page_title="Requests | Management Agent", page_icon="📋", layout="wide")

STATUS_LABELS = {
    "emergency": "🚨 Emergency",
    "needs_human": "🙋 Wants a person",
    "with_staff": "👤 With staff",
    "submitted": "✅ Submitted",
    "ready": "📝 Ready, not submitted",
    "open": "💬 In conversation",
    "resolved": "✔️ Resolved",
    "cancelled": "✖️ Cancelled",
}
# Most urgent first.
STATUS_ORDER = list(STATUS_LABELS)


def when(timestamp: str) -> str:
    return datetime.fromisoformat(timestamp).astimezone().strftime("%d %b %H:%M")


st.title("📋 Resident requests")
st.caption(f"From the Telegram bot · {default_store_path()}")


@st.fragment(run_every="10s")
def requests_view() -> None:
    path = default_store_path()
    if not path.exists():
        st.info("No requests yet. Start the bot and message it on Telegram.")
        return
    store = RequestStore(path)
    records = store.all()
    if not records:
        st.info("No requests yet. Start the bot and message it on Telegram.")
        return

    counts = {status: sum(r.status == status for r in records) for status in STATUS_ORDER}
    columns = st.columns(len(STATUS_ORDER))
    for column, status in zip(columns, STATUS_ORDER, strict=True):
        column.metric(STATUS_LABELS[status], counts[status])

    left, right = st.columns([2, 1])
    with left:
        statuses = st.multiselect(
            "Status",
            STATUS_ORDER,
            default=[s for s in STATUS_ORDER if s not in {"resolved", "cancelled"}],
            format_func=STATUS_LABELS.get,
        )
    with right:
        search = st.text_input("Search", placeholder="Unit, resident or words")

    shown = [r for r in records if r.status in statuses]
    if search:
        needle = search.casefold()
        shown = [
            r
            for r in shown
            if needle in " ".join([r.id, r.resident, r.location or "", *r.messages]).casefold()
        ]
    # Most urgent first, newest first within each status (records arrive newest first).
    shown.sort(key=lambda r: STATUS_ORDER.index(r.status))

    table = pd.DataFrame(
        [
            {
                "ID": r.id,
                "Status": STATUS_LABELS[r.status],
                "Category": LABELS[Category(r.category)].name,
                "Priority": r.priority,
                "Team": r.team,
                "Location": r.location or "—",
                "📷": f"📷 {len(r.photos)}" if r.photos else "",
                "Resident": r.resident,
                "Updated": when(r.updated_at),
            }
            for r in shown
        ]
    )
    picked = st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        key="requests_table",
        on_select="rerun",
        selection_mode="multi-row",
    )
    selected = [shown[i].id for i in picked.selection.rows if i < len(shown)]
    if selected:
        with st.popover(f"🗑️ Delete {len(selected)} selected", type="primary"):
            st.write(f"Delete {', '.join(selected)} and their photos? This can't be undone.")
            # The key names the exact IDs shown, so if a refresh shifts the rows between
            # opening this and clicking, the click is dropped instead of deleting others.
            if st.button("Yes, delete", key="delete-" + "-".join(selected), type="primary"):
                for request_id in selected:
                    store.delete(request_id)
                st.toast(f"Deleted {len(selected)} request(s)")
                st.rerun(scope="fragment")
    else:
        st.caption("Tick rows in the table (left edge) to delete them.")

    st.subheader("Details")
    for r in shown:
        title = f"{STATUS_LABELS[r.status]} · {r.id} · {LABELS[Category(r.category)].name}"
        with st.expander(title):
            st.write(f"**Resident:** {r.resident}")
            st.write(f"**Team:** {r.team} · **Priority:** {r.priority}")
            st.write(f"**Location:** {r.location or 'not given'}")
            if r.missing_details:
                st.write("**Still missing:** " + "; ".join(r.missing_details))
            if r.review_reasons:
                st.write("**Review notes:**")
                for reason in r.review_reasons:
                    st.write(f"- {reason}")
            st.write("**Resident said:**")
            for text in r.messages:
                # Plain text: resident messages are untrusted and must not render as markdown.
                st.text(text)
            if r.photos:
                st.write(f"**Photos ({len(r.photos)}):**")
                columns = st.columns(min(len(r.photos), 3))
                for index, relative in enumerate(r.photos):
                    photo = store.photo_path(relative)
                    try:
                        columns[index % len(columns)].image(str(photo))
                    except Exception:  # a missing or unreadable file shouldn't break the page
                        columns[index % len(columns)].caption(f"Photo unavailable: {relative}")
            st.caption(f"Opened {when(r.created_at)} · updated {when(r.updated_at)}")
            with st.popover("🗑️ Delete"):
                st.write(f"Delete **{r.id}** and its photos? This can't be undone.")
                if st.button("Yes, delete it", key=f"delete-{r.id}", type="primary"):
                    store.delete(r.id)
                    st.toast(f"Deleted {r.id}")
                    st.rerun(scope="fragment")


requests_view()

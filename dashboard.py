# ruff: noqa: E501  (inline CSS and HTML templates)
"""Staff dashboard for requests that residents send through the Telegram bot.

Run with run_dashboard.cmd (Windows), ./run_dashboard.sh or `uv run streamlit run dashboard.py`.
It reads the same SQLite file the bot writes, so run both on the same machine.
Resident text is untrusted: everything from the database is HTML-escaped before rendering.
"""

import base64
import hashlib
import re
from collections import Counter
from datetime import UTC, datetime
from html import escape
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from condo_classifier.icons import icon
from condo_classifier.schema import Category
from condo_classifier.store import RequestRecord, RequestStore, default_store_path
from condo_classifier.taxonomy import LABELS

load_dotenv(Path(__file__).resolve().parent / ".env")
st.set_page_config(
    page_title="Requests · Management Agent", page_icon=":material/apartment:", layout="wide"
)

# Status: label, icon, text colour, tint. Pills always pair the colour with an icon and label.
STATUS = {
    "emergency": ("Emergency", "siren", "#B42318", "#FEE4E2"),
    "needs_human": ("Wants a person", "hand", "#B54708", "#FEF0C7"),
    "with_staff": ("With staff", "headset", "#175CD3", "#D1E9FF"),
    "submitted": ("New work order", "send", "#0E6E55", "#D3F2E6"),
    "ready": ("Awaiting resident", "clipboard-list", "#5925DC", "#EBE9FE"),
    "open": ("In conversation", "message-circle", "#475467", "#EEF1F4"),
    "resolved": ("Resolved", "circle-check", "#067647", "#DCFAE6"),
    "answered": ("Answered by FAQ", "book-open", "#3E4784", "#EEF4FF"),
    "cancelled": ("Cancelled", "circle-x", "#667085", "#EEF1F4"),
}
# Most urgent first.
STATUS_ORDER = list(STATUS)
# Overview groups: label, statuses, colour (swatch and bar segment), footnote builder.
# Finished work is muted so the bar draws the eye to what is still active.
GROUPS = [
    ("Needs attention", {"emergency", "needs_human"}, "#D92D20"),
    ("New work orders", {"submitted"}, "#12917A"),
    ("With staff", {"with_staff"}, "#2E90FA"),
    ("With the bot", {"open", "ready"}, "#98A2B3"),
    ("Resolved", {"resolved", "answered"}, "#A6D8BF"),
]
CATEGORY_ICON = {
    "maintenance": "wrench",
    "security": "shield",
    "noise": "volume-2",
    "cleanliness": "sparkles",
    "facilities": "calendar-days",
    "parking": "car",
    "billing": "receipt",
    "renovation": "hammer",
    "general_enquiry": "info",
    "other": "circle-help",
}
AVATAR_TINTS = ["#DCEFE9", "#E3E8F8", "#F6E7DC", "#EDE5F7", "#E2F0F7", "#F3EEDA"]
BRAND = "#17665E"

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
/* One typeface everywhere except Streamlit's icon glyphs (spans marked translate="no"). */
.stApp, .stApp :is(h1,h2,h3,h4,h5,h6,p,li,label,button,input,textarea,summary,
  span:not([translate="no"])) { font-family: Inter, "Source Sans 3", system-ui, sans-serif; }
.stAppDeployButton, [data-testid="stMainMenu"] { display: none; }
.block-container { padding-top: 1.6rem; max-width: 1360px; }
@media (max-width: 640px) { .block-container { padding-top: 3.4rem; } }
[data-testid="stHeader"] { background: transparent; }
h1, h2, h3 { letter-spacing: -0.02em; }
.ma-head { display:flex; align-items:flex-end; justify-content:space-between; gap:16px; margin:0 0 18px; flex-wrap:wrap; }
.ma-eyebrow { font-size:.72rem; font-weight:600; letter-spacing:.08em; text-transform:uppercase; color:#17665E; margin-bottom:4px; }
.ma-title { font-size:1.6rem; font-weight:700; color:#0F1F1C; letter-spacing:-0.02em; line-height:1.15; }
.ma-live { display:flex; align-items:center; gap:7px; font-size:.8rem; color:#667085; padding-bottom:3px; }
.ma-dot { width:7px; height:7px; border-radius:50%; background:#12B76A; box-shadow:0 0 0 3px #D1FADF; }
.ma-dot.off { background:#98A2B3; box-shadow:0 0 0 3px #EAECF0; }
.ma-overview { background:#FFFFFF; border:1px solid #E1E6E2; border-radius:14px; margin-bottom:22px;
  box-shadow: 0 1px 2px rgba(16,24,40,.04); }
.ma-stats { display:grid; grid-template-columns: 1.3fr repeat(4, 1fr); }
.ma-stat { padding:16px 20px 12px; border-left:1px solid #EDF0EE; min-width:0; }
.ma-stat:first-child { border-left:none; }
.ma-stat-label { display:flex; align-items:center; gap:7px; font-size:.68rem; font-weight:600;
  letter-spacing:.07em; text-transform:uppercase; color:#667085; white-space:nowrap; }
.ma-swatch { width:8px; height:8px; border-radius:2px; flex:none; }
.ma-stat-value { font-size:1.8rem; font-weight:600; color:#0F1F1C; margin-top:8px; line-height:1;
  font-variant-numeric: tabular-nums; letter-spacing:-0.02em; }
.ma-stat-value.alert { color:#B42318; }
.ma-stat-foot { font-size:.78rem; color:#667085; margin-top:6px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.ma-mix { display:flex; gap:2px; margin:4px 20px 16px; }
.ma-seg { height:6px; border-radius:3px; min-width:6px; }
.ma-mix-empty { height:6px; border-radius:3px; background:#EEF1EF; flex:1; }
@media (max-width: 900px) {
  .ma-stats { grid-template-columns: repeat(2, minmax(0,1fr)); }
  .ma-stat { border-left:none; border-top:1px solid #EDF0EE; }
  .ma-stat:first-child { grid-column: 1 / -1; border-top:none; }
}
[class*="st-key-danger"] button { color:#B42318 !important; border-color:#FECDCA !important; }
.ma-card { display:flex; gap:14px; align-items:flex-start; }
.ma-avatar { width:42px; height:42px; border-radius:50%; flex:none; object-fit:cover; display:flex;
  align-items:center; justify-content:center; font-weight:600; font-size:.9rem; color:#1F2A27; }
.ma-main { flex:1; min-width:0; }
.ma-row1 { display:flex; align-items:baseline; gap:8px; flex-wrap:wrap; }
.ma-name { font-weight:600; color:#0F1F1C; }
.ma-muted { color:#667085; font-size:.82rem; }
.stApp span.ma-id { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size:.76rem; color:#667085; }
.ma-meta { display:flex; align-items:center; gap:14px; flex-wrap:wrap; margin-top:4px; font-size:.83rem; color:#344054; }
.ma-meta span { display:inline-flex; align-items:center; gap:5px; }
.ma-preview { margin-top:6px; font-size:.86rem; color:#475467; overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap; }
.ma-side { display:flex; flex-direction:column; align-items:flex-end; gap:6px; flex:none; }
.ma-pill { display:inline-flex; align-items:center; gap:6px; border-radius:999px; padding:3px 10px 3px 8px;
  font-size:.76rem; font-weight:600; white-space:nowrap; }
.ma-chip { display:inline-flex; align-items:center; gap:4px; border-radius:6px; padding:1px 7px;
  font-size:.72rem; font-weight:600; background:#FEF3F2; color:#B42318; border:1px solid #FECDCA; }
.ma-panel-title { display:flex; align-items:center; gap:8px; font-weight:600; color:#0F1F1C; margin:2px 0 10px; }
.ma-bar { display:grid; grid-template-columns: 148px 1fr 28px; align-items:center; gap:10px; margin:7px 0;
  font-size:.82rem; color:#344054; }
.ma-bar-label { display:flex; align-items:center; gap:6px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.ma-track { height:10px; background:#EEF1EF; border-radius:4px; }
.ma-fill { height:10px; background:#17665E; border-radius:0 4px 4px 0; }
.ma-count { text-align:right; color:#0F1F1C; font-weight:600; font-variant-numeric: tabular-nums; }
.ma-bubble { background:#F2F5F3; border:1px solid #E1E6E2; border-radius:12px 12px 12px 4px; padding:8px 12px;
  margin:6px 0; font-size:.88rem; color:#1F2A27; white-space:pre-wrap; max-width: 90%; }
.ma-note { display:flex; gap:8px; align-items:flex-start; font-size:.84rem; color:#344054; margin:4px 0; }
.ma-empty { text-align:center; color:#667085; padding:48px 0; }
@media (max-width: 640px) {
  .ma-card { flex-wrap:wrap; }
  .ma-side { flex-direction:row; align-items:center; width:100%; padding-left:56px; }
  .ma-meta { gap:4px 12px; }
}
</style>
"""


def now() -> datetime:
    return datetime.now(UTC)


def ago(timestamp: str) -> str:
    seconds = max(0, int((now() - datetime.fromisoformat(timestamp)).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return datetime.fromisoformat(timestamp).astimezone().strftime("%d %b")


def resident_parts(r: RequestRecord) -> tuple[str, str | None, int | None]:
    """Name, @username and Telegram ID, also for records saved before these had columns."""
    if r.resident_name:
        return r.resident_name, r.username, r.user_id
    found = re.match(r"^(.*?)(?: @(\w+))? \(Telegram ID (\d+)\)$", r.resident)
    if found:
        return found.group(1), found.group(2), int(found.group(3))
    return r.resident, None, None


def avatar(store: RequestStore, r: RequestRecord) -> str:
    name, _, user_id = resident_parts(r)
    path = store.avatar_path(user_id)
    if path is not None:
        data = base64.b64encode(path.read_bytes()).decode()
        return f'<img class="ma-avatar" src="data:image/jpeg;base64,{data}" alt="">'
    initials = "".join(part[0] for part in name.split()[:2]).upper() or "?"
    tint = AVATAR_TINTS[int(hashlib.sha1(name.encode()).hexdigest(), 16) % len(AVATAR_TINTS)]
    return f'<div class="ma-avatar" style="background:{tint}">{escape(initials)}</div>'


def pill(status: str) -> str:
    label, name, fg, bg = STATUS[status]
    return (
        f'<span class="ma-pill" style="color:{fg};background:{bg}">'
        f"{icon(name, 13, fg, 2.2)}{label}</span>"
    )


def category_name(value: str) -> str:
    return LABELS[Category(value)].name


def card(store: RequestStore, r: RequestRecord) -> str:
    name, username, _ = resident_parts(r)
    handle = f'<span class="ma-muted">@{escape(username)}</span>' if username else ""
    meta = [
        f"<span>{icon(CATEGORY_ICON[r.category], 15, '#475467')}{category_name(r.category)}</span>"
    ]
    meta.append(
        f"<span>{icon('map-pin', 15, '#475467')}{escape(r.location) if r.location else 'No location yet'}</span>"
    )
    meta.append(f"<span>{icon('users', 15, '#475467')}{escape(r.team)}</span>")
    if r.photos:
        meta.append(f"<span>{icon('image', 15, '#475467')}{len(r.photos)}</span>")
    latest = escape(r.messages[-1]) if r.messages else ""
    chips = ""
    # The emergency status pill already says it; flag high priority on other requests.
    if r.priority in {"high", "emergency"} and r.status != "emergency":
        chips = f'<span class="ma-chip">{icon("triangle-alert", 12, "#B42318", 2.4)}{r.priority.title()}</span>'
    return f"""
<div class="ma-card">
  {avatar(store, r)}
  <div class="ma-main">
    <div class="ma-row1"><span class="ma-name">{escape(name)}</span>{handle}<span class="ma-id">{r.id}</span></div>
    <div class="ma-meta">{"".join(meta)}</div>
    <div class="ma-preview">{latest}</div>
  </div>
  <div class="ma-side">{pill(r.status)}{chips}<span class="ma-muted">{ago(r.updated_at)}</span></div>
</div>"""


def overview(records: list[RequestRecord]) -> str:
    """One panel: a figure per group, and a bar of the whole queue's mix underneath."""
    count = Counter(r.status for r in records)
    waiting = [r for r in records if r.status in {"emergency", "needs_human"}]
    oldest = min((r.updated_at for r in waiting), default=None)
    today = now().date()
    resolved_today = sum(
        1
        for r in records
        if r.status == "resolved" and datetime.fromisoformat(r.updated_at).date() == today
    )
    feet = [
        f"{count['emergency']} emergency · oldest {ago(oldest)}" if oldest else "Nothing urgent",
        "Submitted by residents",
        "Replying in Telegram",
        f"{count['ready']} waiting for Submit",
        f"{resolved_today} today · {count['answered']} answered by FAQ",
    ]
    stats, segments = [], []
    for (label, statuses, colour), foot in zip(GROUPS, feet, strict=True):
        value = sum(count[s] for s in statuses)
        alert = " alert" if label == "Needs attention" and value else ""
        stats.append(
            f'<div class="ma-stat"><div class="ma-stat-label"><span class="ma-swatch" '
            f'style="background:{colour}"></span>{label}</div>'
            f'<div class="ma-stat-value{alert}">{value}</div>'
            f'<div class="ma-stat-foot">{escape(foot)}</div></div>'
        )
        if value:
            segments.append(
                f'<div class="ma-seg" style="flex:{value};background:{colour}" '
                f'title="{label}: {value}"></div>'
            )
    mix = "".join(segments) or '<div class="ma-mix-empty"></div>'
    return (
        f'<div class="ma-overview"><div class="ma-stats">{"".join(stats)}</div>'
        f'<div class="ma-mix" role="img" aria-label="Requests by status">{mix}</div></div>'
    )


def bars(title: str, title_icon: str, counts: Counter, label, row_icon) -> str:
    """Horizontal bars, one series in one hue; hover shows the exact share."""
    total = sum(counts.values()) or 1
    top = max(counts.values(), default=1)
    rows = []
    for key, count in counts.most_common():
        share = round(100 * count / total)
        rows.append(
            f'<div class="ma-bar" title="{escape(label(key))}: {count} request(s), {share}%">'
            f'<span class="ma-bar-label">{row_icon(key)}{escape(label(key))}</span>'
            f'<div class="ma-track"><div class="ma-fill" style="width:{max(4, 100 * count / top):.0f}%"></div></div>'
            f'<span class="ma-count">{count}</span></div>'
        )
    body = "".join(rows) or '<div class="ma-muted">No requests yet</div>'
    return f'<div class="ma-panel-title">{icon(title_icon, 16, BRAND)}{title}</div>{body}'


def details(store: RequestStore, r: RequestRecord) -> None:
    _, username, user_id = resident_parts(r)
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.markdown("**Conversation**")
        if r.messages:
            st.html("".join(f'<div class="ma-bubble">{escape(text)}</div>' for text in r.messages))
        else:
            st.caption("No messages saved.")
    with right:
        urgent = r.priority in {"high", "emergency"}
        opened = datetime.fromisoformat(r.created_at).astimezone().strftime("%d %b %Y, %H:%M")
        facts = [
            ("users", "#667085", "Team", r.team),
            (
                "triangle-alert" if urgent else "activity",
                "#B42318" if urgent else "#667085",
                "Priority",
                r.priority.title(),
            ),
            ("map-pin", "#667085", "Location", r.location or "Not given"),
            ("clock", "#667085", "Opened", opened),
        ]
        if username:
            facts.append(("message-circle", "#667085", "Telegram", f"@{username}"))
        if user_id:
            facts.append(("info", "#667085", "Telegram ID", str(user_id)))
        notes = "".join(
            f'<div class="ma-note">{icon(i, 15, colour)}<span><b>{label}:</b> {escape(value)}</span></div>'
            for i, colour, label, value in facts
        )
        notes += "".join(
            f'<div class="ma-note">{icon("triangle-alert", 15, "#B54708")}<span><b>Missing:</b> {escape(m)}</span></div>'
            for m in r.missing_details
        )
        for note in r.review_reasons:
            confirmed = note.startswith("✅")
            text = note.removeprefix("✅").strip()
            mark = icon("circle-check", 15, "#067647") if confirmed else icon("info", 15, "#667085")
            notes += f'<div class="ma-note">{mark}<span>{escape(text)}</span></div>'
        st.html(notes)
        if r.photos:
            st.markdown(f"**Photos** ({len(r.photos)})")
            columns = st.columns(2)
            for index, relative in enumerate(r.photos):
                try:
                    columns[index % 2].image(str(store.photo_path(relative)), width=220)
                except Exception:  # a missing or unreadable file shouldn't break the page
                    columns[index % 2].caption(f"Photo unavailable: {relative}")
        st.divider()
        with st.container(key=f"danger-{r.id}"):
            with st.popover("Delete request", icon=":material/delete:"):
                st.write(f"Delete **{r.id}** and its photos? This can't be undone.")
                if st.button("Yes, delete it", key=f"delete-{r.id}", type="primary"):
                    store.delete(r.id)
                    st.toast(f"Deleted {r.id}", icon=":material/delete:")
                    st.rerun(scope="fragment")


def table_view(store: RequestStore, shown: list[RequestRecord]) -> None:
    table = pd.DataFrame(
        [
            {
                "ID": r.id,
                "Status": STATUS[r.status][0],
                "Category": category_name(r.category),
                "Priority": r.priority.title(),
                "Team": r.team,
                "Location": r.location or "",
                "Photos": str(len(r.photos)) if r.photos else "",
                "Resident": resident_parts(r)[0],
                "Updated": datetime.fromisoformat(r.updated_at)
                .astimezone()
                .strftime("%d %b %H:%M"),
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
        with st.popover(
            f"Delete {len(selected)} selected", icon=":material/delete:", type="primary"
        ):
            st.write(f"Delete {', '.join(selected)} and their photos? This can't be undone.")
            # The key names the exact IDs shown, so if a refresh shifts the rows between
            # opening this and clicking, the click is dropped instead of deleting others.
            if st.button("Yes, delete", key="delete-" + "-".join(selected), type="primary"):
                for request_id in selected:
                    store.delete(request_id)
                st.toast(f"Deleted {len(selected)} request(s)", icon=":material/delete:")
                st.rerun(scope="fragment")
    else:
        st.caption("Tick rows (left edge) to delete several requests at once.")


def header(live: bool) -> None:
    dot = "ma-dot" if live else "ma-dot off"
    state = f"Live · updated {datetime.now().strftime('%H:%M:%S')}" if live else "Paused"
    st.html(
        f"""<div class="ma-head">
  <div><div class="ma-eyebrow">Management Agent</div><div class="ma-title">Resident requests</div></div>
  <div class="ma-live"><span class="{dot}"></span>{state}</div>
</div>"""
    )


def view(live: bool, statuses: list[str], categories: list[str], search: str) -> None:
    header(live)
    path = default_store_path()
    if not path.exists():
        st.html(
            f'<div class="ma-empty">{icon("inbox", 32, "#98A2B3")}<br>No requests yet. Start the bot and message it on Telegram.</div>'
        )
        return
    store = RequestStore(path)
    records = store.all()
    st.html(overview(records))

    shown = [r for r in records if r.status in statuses]
    if categories:
        shown = [r for r in shown if r.category in categories]
    if search:
        needle = search.casefold()
        shown = [
            r
            for r in shown
            if needle in " ".join([r.id, r.resident, r.location or "", *r.messages]).casefold()
        ]
    # Most urgent first, newest first within each status (records arrive newest first).
    shown.sort(key=lambda r: STATUS_ORDER.index(r.status))

    main, side = st.columns([2.4, 1], gap="large")
    with side:
        st.html('<div style="height:2.9rem"></div>')  # line up with the tab row
        with st.container(border=True):
            st.html(
                bars(
                    "By category",
                    "activity",
                    Counter(r.category for r in records),
                    category_name,
                    lambda c: icon(CATEGORY_ICON[c], 14, "#475467"),
                )
            )
        with st.container(border=True):
            st.html(bars("By team", "users", Counter(r.team for r in records), str, lambda t: ""))
    with main:
        queue, table = st.tabs(
            [f":material/view_agenda: Queue ({len(shown)})", ":material/table_rows: Table"]
        )
        with queue:
            if not shown:
                st.html(
                    f'<div class="ma-empty">{icon("inbox", 32, "#98A2B3")}<br>No requests match these filters.</div>'
                )
            for r in shown:
                with st.container(border=True, key=f"card-{r.id}"):
                    st.html(card(store, r))
                    with st.expander("Conversation and details", icon=":material/forum:"):
                        details(store, r)
        with table:
            table_view(store, shown)


st.html(CSS)
with st.sidebar:
    st.markdown("### :material/tune: Filters")
    statuses = st.pills(
        "Status",
        STATUS_ORDER,
        selection_mode="multi",
        default=[s for s in STATUS_ORDER if s not in {"resolved", "cancelled"}],
        format_func=lambda s: STATUS[s][0],
    )
    categories = st.multiselect("Category", [c.value for c in Category], format_func=category_name)
    search = st.text_input("Search", placeholder="Unit, resident, request ID or words")
    st.divider()
    live = st.toggle(
        "Live refresh", value=True, help="Reloads every 15 seconds. Pause it while deleting."
    )
    st.caption(f"Database: {default_store_path().name}", help=str(default_store_path()))

st.fragment(run_every=15 if live else None)(view)(live, statuses, categories, search)

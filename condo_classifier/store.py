"""SQLite record of resident requests, shared by the Telegram bot and the staff dashboard."""

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

STATUSES = (
    "open",
    "ready",
    "submitted",
    "needs_human",
    "with_staff",
    "in_progress",
    "resolved",
    "emergency",
    "cancelled",
)

# What staff can set from the dashboard: Open (a work order nobody has picked up yet),
# In progress and Done. Each maps to a stored status.
STAFF_STATUSES = {"submitted": "Open", "in_progress": "In progress", "resolved": "Done"}
# Statuses only staff set. The bot's routine statuses don't overwrite them, so a resident's
# follow-up message can't undo a staff change; an emergency or a request for a person still can.
STAFF_SET = {"in_progress", "resolved"}
ROUTINE = {"open", "ready", "submitted"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    resident TEXT NOT NULL,
    status TEXT NOT NULL,
    category TEXT NOT NULL,
    priority TEXT NOT NULL,
    team TEXT NOT NULL,
    location TEXT,
    missing_details TEXT NOT NULL,
    review_reasons TEXT NOT NULL,
    messages TEXT NOT NULL,
    photos TEXT NOT NULL DEFAULT '[]',
    user_id INTEGER,
    resident_name TEXT,
    username TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class RequestRecord:
    id: str
    chat_id: int
    resident: str
    status: str
    category: str
    priority: str
    team: str
    location: str | None
    missing_details: list[str] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    # Paths relative to the store's folder, e.g. "photos/R-1A2B3C/1.jpg".
    photos: list[str] = field(default_factory=list)
    # The resident's Telegram account, kept separately for the dashboard (avatars, handles).
    user_id: int | None = None
    resident_name: str | None = None
    username: str | None = None
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)


LIST_FIELDS = ("missing_details", "review_reasons", "messages", "photos")
ADDED_COLUMNS = {
    "photos": "TEXT NOT NULL DEFAULT '[]'",
    "user_id": "INTEGER",
    "resident_name": "TEXT",
    "username": "TEXT",
}


class RequestStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(SCHEMA)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(requests)")}
            # Columns added after the first release; older databases gain them on start.
            for name, ddl in ADDED_COLUMNS.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE requests ADD COLUMN {name} {ddl}")

    def save_photo(self, request_id: str, jpeg: bytes) -> str:
        """Write a photo next to the database and return its relative path."""
        if not request_id.replace("-", "").isalnum():
            raise ValueError("Unexpected request ID.")
        folder = self.path.parent / "photos" / request_id
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{len(list(folder.glob('*.jpg'))) + 1}.jpg"
        (folder / name).write_bytes(jpeg)
        return f"photos/{request_id}/{name}"

    def delete(self, request_id: str) -> bool:
        """Remove a request and its saved photos. Returns False if it didn't exist."""
        with self._connect() as db:
            deleted = db.execute("DELETE FROM requests WHERE id = ?", (request_id,)).rowcount
        folder = self.path.parent / "photos" / request_id
        if request_id.replace("-", "").isalnum() and folder.is_dir():
            for photo in folder.iterdir():
                photo.unlink()
            folder.rmdir()
        return bool(deleted)

    def save_avatar(self, user_id: int, jpeg: bytes) -> None:
        folder = self.path.parent / "avatars"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{int(user_id)}.jpg").write_bytes(jpeg)

    def avatar_path(self, user_id: int | None) -> Path | None:
        """The resident's saved Telegram profile photo, if the bot could fetch one."""
        if user_id is None:
            return None
        path = self.path.parent / "avatars" / f"{int(user_id)}.jpg"
        return path if path.exists() else None

    def photo_path(self, relative: str) -> Path:
        return self.path.parent / relative

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # A short-lived connection per call keeps this safe across threads and processes.
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:  # commits on success, rolls back on error
                yield db
        finally:
            db.close()

    def save(self, record: RequestRecord) -> None:
        if record.status not in STATUSES:
            raise ValueError(f"Unknown status: {record.status}")
        record.updated_at = now()
        row = asdict(record)
        for key in LIST_FIELDS:
            row[key] = json.dumps(row[key], ensure_ascii=False)
        columns = ", ".join(row)
        placeholders = ", ".join(f":{key}" for key in row)
        updates = ", ".join(f"{key} = excluded.{key}" for key in row if key != "created_at")
        with self._connect() as db:
            db.execute(
                f"INSERT INTO requests ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                row,
            )

    def set_status(self, request_id: str, status: str) -> bool:
        """Change only a request's status, as staff do from the dashboard. Returns False if
        the request no longer exists."""
        if status not in STATUSES:
            raise ValueError(f"Unknown status: {status}")
        with self._connect() as db:
            changed = db.execute(
                "UPDATE requests SET status = ?, updated_at = ? WHERE id = ?",
                (status, now(), request_id),
            ).rowcount
        return bool(changed)

    def get(self, request_id: str) -> RequestRecord | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        return self._record(row) if row else None

    def all(self) -> list[RequestRecord]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM requests ORDER BY updated_at DESC").fetchall()
        return [self._record(row) for row in rows]

    @staticmethod
    def _record(row: sqlite3.Row) -> RequestRecord:
        data = dict(row)
        for key in LIST_FIELDS:
            data[key] = json.loads(data[key])
        return RequestRecord(**data)


def default_store_path() -> Path:
    """REQUESTS_DB from .env, or data/private/requests.db (gitignored) in the project."""
    root = Path(__file__).resolve().parents[1]
    return Path(os.getenv("REQUESTS_DB") or root / "data" / "private" / "requests.db")

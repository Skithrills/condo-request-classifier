import asyncio
import base64
import io
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import anthropic
import httpx2
import pytest
from PIL import Image

from condo_classifier.service import Classifier
from condo_classifier.store import RequestStore
from condo_classifier.vision import ClaudePhotoDescriber, photo_message, prepare_image


def jpeg(width=4000, height=3000, color=(120, 90, 60), exif_rotate=False):
    image = Image.new("RGB", (width, height), color)
    out = io.BytesIO()
    if exif_rotate:
        exif = Image.Exif()
        exif[0x0112] = 6  # phone held upright: rotate 90°
        image.save(out, format="JPEG", exif=exif)
    else:
        image.save(out, format="JPEG")
    return out.getvalue()


def size(data):
    with Image.open(io.BytesIO(data)) as image:
        return image.size


def test_large_photos_shrink_to_the_limit_and_small_ones_stay():
    assert size(prepare_image(jpeg(4000, 3000), 1568)) == (1568, 1176)
    assert size(prepare_image(jpeg(800, 600), 1568)) == (800, 600)


def test_phone_rotation_is_applied():
    assert size(prepare_image(jpeg(400, 300, exif_rotate=True), 1568)) == (300, 400)


def test_png_with_transparency_becomes_jpeg():
    out = io.BytesIO()
    Image.new("RGBA", (50, 50), (0, 0, 0, 0)).save(out, format="PNG")
    assert prepare_image(out.getvalue(), 1568)[:2] == b"\xff\xd8"


def test_photo_message_keeps_caption_first():
    assert photo_message("Mould on ceiling.", "bathroom #03-13") == (
        "bathroom #03-13\n[Photo: Mould on ceiling.]"
    )
    assert photo_message("Mould on ceiling.", None) == "[Photo: Mould on ceiling.]"


def response(text="Black mould spreading on a bathroom ceiling.", stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        usage=SimpleNamespace(input_tokens=1500, output_tokens=40),
    )


@pytest.fixture
def describer(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    describer = ClaudePhotoDescriber()
    describer.client = Mock()
    return describer


def test_describer_sends_resized_jpeg_and_caption(describer):
    describer.client.messages.create.return_value = response()
    text = describer.describe(jpeg(4000, 3000), "my bathroom")
    assert text == "Black mould spreading on a bathroom ceiling."
    kwargs = describer.client.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-haiku-5-5"
    assert "temperature" not in kwargs
    image, caption = kwargs["messages"][0]["content"]
    assert image["source"]["media_type"] == "image/jpeg"
    assert size(base64.b64decode(image["source"]["data"])) == (1568, 1176)
    assert caption["text"] == "The resident's caption: my bathroom"
    assert describer.input_tokens == 1500


@pytest.mark.parametrize(
    "outcome",
    [
        response(stop_reason="refusal"),
        response(text="  "),
        anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com")),
    ],
)
def test_describer_failures_return_none(describer, outcome):
    if isinstance(outcome, Exception):
        describer.client.messages.create.side_effect = outcome
    else:
        describer.client.messages.create.return_value = outcome
    assert describer.describe(jpeg(100, 100)) is None


def test_unreadable_file_is_not_sent(describer):
    assert describer.describe(b"not an image") is None
    describer.client.messages.create.assert_not_called()


@pytest.mark.parametrize("edge", ["100", "9000"])
def test_image_edge_setting_is_checked(monkeypatch, edge):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("IMAGE_MAX_EDGE", edge)
    with pytest.raises(ValueError, match="IMAGE_MAX_EDGE"):
        ClaudePhotoDescriber.from_env()


def test_old_database_gets_a_photos_column(tmp_path):
    path = tmp_path / "requests.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE requests (id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, "
            "resident TEXT NOT NULL, status TEXT NOT NULL, category TEXT NOT NULL, "
            "priority TEXT NOT NULL, team TEXT NOT NULL, location TEXT, "
            "missing_details TEXT NOT NULL, review_reasons TEXT NOT NULL, "
            "messages TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        db.execute(
            "INSERT INTO requests VALUES ('R-1', 1, 'Jane', 'open', 'noise', 'normal', "
            "'Resident relations', NULL, '[]', '[]', '[\"hi\"]', '2026-10-14T00:00:00+00:00', "
            "'2026-10-14T00:00:00+00:00')"
        )
    db.close()
    assert RequestStore(path).get("R-1").photos == []


def test_photos_are_saved_next_to_the_database(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    first = store.save_photo("R-ABC123", b"one")
    second = store.save_photo("R-ABC123", b"two")
    assert (first, second) == ("photos/R-ABC123/1.jpg", "photos/R-ABC123/2.jpg")
    assert store.photo_path(second).read_bytes() == b"two"
    with pytest.raises(ValueError):
        store.save_photo("../../etc", b"x")


# Bot flow -----------------------------------------------------------------------------

telegram = pytest.importorskip("telegram")
from telegram.error import Forbidden  # noqa: E402

from condo_classifier.telegram_bot import ResidentBot  # noqa: E402

USER = SimpleNamespace(full_name="Jane Tan", username="janet", id=42)


class FakeDescriber:
    def __init__(self, text="Black mould spreading across a bathroom ceiling."):
        self.text = text
        self.calls = []

    def describe(self, image, caption=None):
        self.calls.append(caption)
        return self.text


class FakeChat:
    def __init__(self):
        self.texts = []

    def reply(self, conversation, text, result):
        self.texts.append(text)
        return "Got it."


def fake_tg():
    return SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=7)),
        send_photo=AsyncMock(),
        edit_message_text=AsyncMock(),
        send_chat_action=AsyncMock(),
        set_message_reaction=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )


def send_photo(bot, tg, caption=None, data=None, file_size=200_000):
    data = data if data is not None else jpeg(1280, 960)
    file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(data)))
    photo = SimpleNamespace(file_size=file_size, get_file=AsyncMock(return_value=file))
    message = SimpleNamespace(
        photo=[photo],
        document=None,
        caption=caption,
        message_id=300,
        reply_text=AsyncMock(return_value=SimpleNamespace(message_id=301)),
    )
    update = SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=1, type="private"),
        effective_user=USER,
    )
    asyncio.run(bot.photo(update, SimpleNamespace(bot=tg)))
    return message.reply_text.call_args.args[0]


def make_bot(baseline, tmp_path, describer=None):
    store = RequestStore(tmp_path / "requests.db")
    chat = FakeChat()
    bot = ResidentBot(Classifier(baseline), chat, -100, store, describer or FakeDescriber())
    return bot, store, chat


def test_photo_with_caption_becomes_a_described_request(baseline, tmp_path):
    bot, store, chat = make_bot(baseline, tmp_path)
    tg = fake_tg()
    send_photo(bot, tg, caption="this is my bathroom at #03-13")
    conversation = bot.conversations[1]
    assert conversation.resident_texts == [
        "this is my bathroom at #03-13\n[Photo: Black mould spreading across a bathroom ceiling.]"
    ]
    assert chat.texts == [conversation.resident_texts[0]]
    record = store.get(conversation.request_id)
    assert record.category == "cleanliness" or record.category == "maintenance"
    assert record.photos == [f"photos/{conversation.request_id}/1.jpg"]
    assert size(store.photo_path(record.photos[0]).read_bytes()) == (1280, 960)
    # The staff card goes first, then the photo as a reply to it.
    tg.send_message.assert_awaited_once()
    assert tg.send_photo.call_args.kwargs["reply_to_message_id"] == 7
    assert "[Photo: Black mould" in tg.send_message.call_args.args[1]


def test_photo_without_caption_then_text_detail(baseline, tmp_path):
    bot, store, _ = make_bot(baseline, tmp_path)
    tg = fake_tg()
    send_photo(bot, tg)
    request_id = bot.conversations[1].request_id
    assert bot.conversations[1].resident_texts == [
        "[Photo: Black mould spreading across a bathroom ceiling.]"
    ]
    tg.send_photo.assert_awaited_once()
    # A second photo in the same request is saved and sent once, not resent.
    send_photo(bot, tg)
    assert store.get(request_id).photos[-1].endswith("/2.jpg")
    assert tg.send_photo.await_count == 2


def test_photo_of_fire_escalates_without_waiting(baseline, tmp_path):
    bot, store, chat = make_bot(
        baseline, tmp_path, FakeDescriber("Thick smoke is pouring from a kitchen stove.")
    )
    tg = fake_tg()
    reply = send_photo(bot, tg)
    assert "emergency number" in reply
    assert chat.texts == []
    assert store.all()[0].status == "emergency"
    tg.set_message_reaction.assert_not_awaited()


class UnclearBackend:
    """Stands in for a model backend that reads an unrelated photo as "other"."""

    def predict(self, request):
        from condo_classifier.schema import Category, Prediction

        return Prediction(
            category=Category.OTHER, confidence_kind="unavailable", backend="test", model="test"
        )


def test_unrelated_photo_is_not_saved_until_understood(tmp_path):
    bot, store, _ = make_bot(UnclearBackend(), tmp_path, FakeDescriber("A cat sitting on a sofa."))
    tg = fake_tg()
    send_photo(bot, tg)
    assert store.all() == []
    assert not (tmp_path / "photos").exists()
    tg.send_photo.assert_not_awaited()


def test_photo_problems_get_a_text_fallback(baseline, tmp_path):
    bot, store, _ = make_bot(baseline, tmp_path, FakeDescriber(text=None))
    tg = fake_tg()
    assert "describe the problem in text" in send_photo(bot, tg)
    assert "too big" in send_photo(bot, tg, file_size=50 * 1024 * 1024)
    no_vision = ResidentBot(Classifier(baseline), FakeChat(), None, store, None)
    assert "can't look at photos" in send_photo(no_vision, tg)
    assert "describe the problem in text" in send_photo(bot, tg, data=b"garbage")
    assert store.all() == []


def test_delete_removes_request_and_photos(tmp_path):
    from condo_classifier.store import RequestRecord

    store = RequestStore(tmp_path / "requests.db")
    photo = store.save_photo("R-DEL1", b"jpeg")
    record = RequestRecord(
        "R-DEL1", 1, "Jane", "open", "noise", "normal", "Resident relations", None, photos=[photo]
    )
    store.save(record)
    store.save(RequestRecord("R-KEEP", 1, "Alex", "open", "noise", "normal", "Team", None))
    assert store.delete("R-DEL1") is True
    assert store.get("R-DEL1") is None
    assert not (tmp_path / "photos" / "R-DEL1").exists()
    assert [r.id for r in store.all()] == ["R-KEEP"]
    assert store.delete("R-DEL1") is False


def test_old_database_gains_resident_columns(tmp_path):
    from condo_classifier.store import RequestRecord

    path = tmp_path / "requests.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE requests (id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, "
            "resident TEXT NOT NULL, status TEXT NOT NULL, category TEXT NOT NULL, "
            "priority TEXT NOT NULL, team TEXT NOT NULL, location TEXT, "
            "missing_details TEXT NOT NULL, review_reasons TEXT NOT NULL, "
            "messages TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
    db.close()
    store = RequestStore(path)
    store.save(
        RequestRecord(
            "R-1",
            1,
            "Jane",
            "open",
            "noise",
            "normal",
            "T",
            None,
            user_id=9,
            resident_name="Jane",
            username="jt",
        )
    )
    saved = store.get("R-1")
    assert (saved.user_id, saved.resident_name, saved.username, saved.photos) == (
        9,
        "Jane",
        "jt",
        [],
    )


def test_bot_saves_profile_photo_once(baseline, tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    bot = ResidentBot(Classifier(baseline), FakeChat(), None, store, FakeDescriber())
    small = SimpleNamespace(
        width=160,
        get_file=AsyncMock(
            return_value=SimpleNamespace(
                download_as_bytearray=AsyncMock(return_value=bytearray(jpeg(640, 640)))
            )
        ),
    )
    tg = SimpleNamespace(
        get_user_profile_photos=AsyncMock(
            return_value=SimpleNamespace(total_count=1, photos=[[SimpleNamespace(width=80), small]])
        )
    )
    user = SimpleNamespace(id=42)
    asyncio.run(bot.fetch_avatar(tg, user))
    asyncio.run(bot.fetch_avatar(tg, user))
    assert size(store.avatar_path(42).read_bytes()) == (256, 256)
    tg.get_user_profile_photos.assert_awaited_once()
    # Hidden photo or an API error: no avatar, no crash.
    hidden = SimpleNamespace(
        get_user_profile_photos=AsyncMock(return_value=SimpleNamespace(total_count=0, photos=[]))
    )
    asyncio.run(bot.fetch_avatar(hidden, SimpleNamespace(id=43)))
    broken = SimpleNamespace(get_user_profile_photos=AsyncMock(side_effect=Forbidden("nope")))
    asyncio.run(bot.fetch_avatar(broken, SimpleNamespace(id=44)))
    assert store.avatar_path(43) is None and store.avatar_path(44) is None

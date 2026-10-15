"""Staff changing a request's status from the dashboard."""

import pytest

from condo_classifier.backends import ROOT
from condo_classifier.store import RequestRecord, RequestStore

pytest.importorskip("telegram")
from test_relay import fake_tg, make_bot, resident_says  # noqa: E402


def record(request_id="R-AAA111", status="submitted"):
    return RequestRecord(request_id, 1, "Jane", status, "maintenance", "normal", "T", "Unit 5")


def test_set_status_changes_only_the_status(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    store.save(record())
    before = store.get("R-AAA111")
    assert store.set_status("R-AAA111", "in_progress")
    after = store.get("R-AAA111")
    assert after.status == "in_progress"
    assert after.updated_at >= before.updated_at
    assert {**vars(after), "status": "submitted", "updated_at": ""} == {
        **vars(before),
        "updated_at": "",
    }


def test_set_status_rejects_unknown_status_and_missing_request(tmp_path):
    store = RequestStore(tmp_path / "requests.db")
    store.save(record())
    with pytest.raises(ValueError):
        store.set_status("R-AAA111", "on_fire")
    assert not store.set_status("R-NOPE00", "resolved")
    assert store.get("R-AAA111").status == "submitted"


def test_resident_follow_up_keeps_staff_status(baseline, tmp_path):
    bot, store, _ = make_bot(baseline, tmp_path)
    tg = fake_tg()
    resident_says(bot, tg, "The corridor light keeps flickering", 100)
    request_id = bot.conversations[42].request_id
    store.set_status(request_id, "in_progress")

    resident_says(bot, tg, "It's on level 5 near the lift", 110)
    assert store.get(request_id).status == "in_progress"
    assert bot.conversations[42].status == "in_progress"
    assert tg.edit_message_text.call_args.args[0].startswith("🔧 In progress")


def test_emergency_still_overrides_staff_status(baseline, tmp_path):
    bot, store, _ = make_bot(baseline, tmp_path)
    tg = fake_tg()
    resident_says(bot, tg, "The corridor light keeps flickering", 100)
    request_id = bot.conversations[42].request_id
    store.set_status(request_id, "resolved")

    resident_says(bot, tg, "Now there's smoke coming out of it, there's a fire", 110)
    assert store.get(request_id).status == "emergency"


def test_dashboard_sets_status(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    store = RequestStore(tmp_path / "requests.db")
    store.save(record())
    monkeypatch.setenv("REQUESTS_DB", str(store.path))
    app = AppTest.from_file(ROOT / "dashboard.py", default_timeout=30).run()
    assert not app.exception
    control = app.get("button_group")[0]
    assert control.value == "submitted"
    control.set_value("in_progress").run()
    assert not app.exception
    assert store.get("R-AAA111").status == "in_progress"

    # The control now starts from the new status, and Done works the same way.
    control = app.get("button_group")[0]
    assert control.value == "in_progress"
    control.set_value("resolved").run()
    assert store.get("R-AAA111").status == "resolved"


def test_dashboard_status_control_is_blank_for_bot_statuses(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    store = RequestStore(tmp_path / "requests.db")
    store.save(record(status="needs_human"))
    monkeypatch.setenv("REQUESTS_DB", str(store.path))
    app = AppTest.from_file(ROOT / "dashboard.py", default_timeout=30).run()
    assert not app.exception
    assert app.get("button_group")[0].value is None

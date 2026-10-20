from streamlit.testing.v1 import AppTest

from condo_classifier.backends import ROOT


def button(app, label):
    return next(item for item in app.button if item.label == label)


def test_streamlit_classify_and_example_change():
    app = AppTest.from_file(ROOT / "app.py", default_timeout=30).run()
    assert not app.exception
    next(item for item in app.text_input if item.label == "Location (optional)").set_value(
        "Block A"
    )
    button(app, "Classify request").click().run()
    assert not app.exception
    assert app.session_state.single_result["category"] == "maintenance"
    example = next(item for item in app.selectbox if item.label == "Try an example")
    example.select("Emergency report").run()
    assert app.session_state.request_location == ""
    button(app, "Classify request").click().run()
    assert not app.exception
    assert app.session_state.single_result["priority"] == "emergency"
    assert any("does not dispatch" in item.value for item in app.error)


def test_streamlit_evaluation_and_invalid_input():
    app = AppTest.from_file(ROOT / "app.py", default_timeout=30).run()
    button(app, "Evaluate offline baseline").click().run()
    assert not app.exception
    assert app.session_state.evaluation["rows"] == 55
    app.text_area[0].set_value("   ")
    button(app, "Classify request").click().run()
    assert not app.exception
    assert app.error


def test_changing_engine_clears_result_from_previous_backend():
    app = AppTest.from_file(ROOT / "app.py", default_timeout=30).run()
    button(app, "Classify request").click().run()
    assert app.session_state.single_result["backend"] == "baseline"
    next(item for item in app.selectbox if item.label == "Classification engine").select(
        "Ollama"
    ).run()
    assert not app.exception
    assert "single_result" not in app.session_state


def test_dashboard_table_converts_cleanly_with_and_without_photos(tmp_path, monkeypatch):
    import io

    import pyarrow as pa
    from PIL import Image
    from streamlit.testing.v1 import AppTest

    from condo_classifier.store import RequestRecord, RequestStore

    store = RequestStore(tmp_path / "requests.db")
    jpeg = io.BytesIO()
    Image.new("RGB", (40, 30), (90, 110, 80)).save(jpeg, "JPEG")
    photo = store.save_photo("R-AAA111", jpeg.getvalue())
    store.save(
        RequestRecord("R-AAA111", 1, "Jane", "open", "noise", "normal", "T", None, photos=[photo])
    )
    store.save(RequestRecord("R-BBB222", 1, "Alex", "open", "noise", "normal", "T", None))
    monkeypatch.setenv("REQUESTS_DB", str(store.path))
    app = AppTest.from_file(ROOT / "dashboard.py", default_timeout=30).run()
    assert not app.exception
    table = app.dataframe[0].value
    # Streamlit only falls back (and logs a traceback each refresh) if this raises.
    pa.Table.from_pandas(table)
    assert sorted(table["Photos"]) == ["", "1"]


def test_dashboard_survives_a_broken_photo_file(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from condo_classifier.store import RequestRecord, RequestStore

    store = RequestStore(tmp_path / "requests.db")
    broken = store.save_photo("R-CCC333", b"not an image")
    store.save(
        RequestRecord(
            "R-CCC333",
            1,
            "Jane",
            "open",
            "noise",
            "normal",
            "T",
            None,
            photos=[broken, "photos/R-CCC333/9.jpg"],
        )
    )
    monkeypatch.setenv("REQUESTS_DB", str(store.path))
    app = AppTest.from_file(ROOT / "dashboard.py", default_timeout=30).run()
    assert not app.exception
    assert sum("Photo unavailable" in c.value for c in app.caption) == 2


def dashboard_with(tmp_path, monkeypatch, *records, avatar_for=None):
    from streamlit.testing.v1 import AppTest

    from condo_classifier.store import RequestStore

    store = RequestStore(tmp_path / "requests.db")
    for record in records:
        store.save(record)
    if avatar_for is not None:
        store.save_avatar(avatar_for, b"jpeg-bytes")
    monkeypatch.setenv("REQUESTS_DB", str(store.path))
    return AppTest.from_file(ROOT / "dashboard.py", default_timeout=30).run()


def all_html(app):
    return "".join(element.proto.body for element in app.get("html"))


def test_dashboard_escapes_resident_text(tmp_path, monkeypatch):
    from condo_classifier.store import RequestRecord

    record = RequestRecord(
        "R-XSS001",
        1,
        "Eve <b>Bad</b> (Telegram ID 5)",
        "open",
        "noise",
        "normal",
        "T",
        "<img src=x onerror=alert(1)>",
        messages=["<script>alert('hi')</script>"],
        user_id=5,
        resident_name="Eve <b>Bad</b>",
    )
    app = dashboard_with(tmp_path, monkeypatch, record)
    assert not app.exception
    html = all_html(app)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "onerror=alert" not in html.replace("&lt;img src=x onerror=alert(1)&gt;", "")
    assert "Eve &lt;b&gt;Bad&lt;/b&gt;" in html


def test_dashboard_shows_avatar_or_initials(tmp_path, monkeypatch):
    from condo_classifier.store import RequestRecord

    with_photo = RequestRecord(
        "R-AV0001",
        1,
        "Jane Tan (Telegram ID 7)",
        "open",
        "noise",
        "normal",
        "T",
        None,
        messages=["hi there"],
        user_id=7,
        resident_name="Jane Tan",
    )
    # Saved before the resident columns existed: details come from the label.
    old = RequestRecord(
        "R-AV0002",
        1,
        "Alex Lim @alexl (Telegram ID 8)",
        "open",
        "noise",
        "normal",
        "T",
        None,
        messages=["hello"],
    )
    app = dashboard_with(tmp_path, monkeypatch, with_photo, old, avatar_for=7)
    html = all_html(app)
    assert 'class="ma-avatar" src="data:image/jpeg;base64,' in html
    assert ">AL</div>" in html and "@alexl" in html


def test_icons_are_valid_svg():
    import xml.dom.minidom

    from condo_classifier.icons import PATHS, icon, svg

    for name in PATHS:
        xml.dom.minidom.parseString(svg(name))
    assert icon("wrench").startswith('<img src="data:image/svg+xml;base64,')

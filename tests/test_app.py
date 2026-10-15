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
    assert sorted(table["📷"]) == ["", "📷 1"]


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

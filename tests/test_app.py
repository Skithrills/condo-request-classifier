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

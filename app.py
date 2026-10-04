import hashlib
import json
import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from pydantic import ValidationError

from condo_classifier.backends import ROOT, BaselineBackend, OllamaBackend
from condo_classifier.batch import classify_batch, export_csv, parse_batch, spreadsheet_cell
from condo_classifier.evaluation import evaluate
from condo_classifier.schema import ResidentRequest
from condo_classifier.service import Classifier
from condo_classifier.taxonomy import LABELS

load_dotenv(ROOT / ".env")
st.set_page_config(
    page_title="Management Agent | Request classifier", page_icon="🏡", layout="wide"
)
st.html("""
<style>
    .block-container { max-width: 1240px; padding-top: 2.5rem; }
    [data-testid="stSidebar"] { border-right: 1px solid #d4ded6; }
    h1 { letter-spacing: -0.04em; }
    [data-testid="stMetricValue"] { font-size: 1.6rem; }
    .eyebrow { font-size: .76rem; letter-spacing: .16em; font-weight: 700; color: #17665e; }
    .intro { font-size: 1.08rem; color: #52665d; max-width: 760px; }
    .brand { font-size: 1.1rem; font-weight: 750; line-height: 1.4; }
    @media (max-width: 1100px) {
        [data-testid="stMain"] [data-testid="stHorizontalBlock"] { flex-wrap: wrap; }
        [data-testid="stMain"] [data-testid="stColumn"] {
            flex: 1 1 260px;
            min-width: min(100%, 260px);
        }
    }
</style>
""")


@st.cache_resource
def baseline() -> BaselineBackend:
    return BaselineBackend()


with st.sidebar:
    st.html('<div class="brand">MANAGEMENT<br>AGENT</div>')
    st.caption("Condominium operations · Phase 01")
    st.divider()
    backend_name = st.selectbox("Classification engine", ["Offline baseline", "Ollama"])
    threshold = st.slider("Review threshold", 0.0, 1.0, 0.60, 0.05)
    st.caption("Scores below this threshold need review. Scores are not validated accuracy.")
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    model = os.getenv("OLLAMA_MODEL", "gemma3:4b")
    if backend_name == "Ollama":
        host = st.text_input("Ollama server", value=host)
        model = st.text_input("Model name", value=model)
        st.caption("Cloud credentials are read from OLLAMA_API_KEY in your local .env file.")
        st.info(
            "Ollama sends entered text to the configured server. All LLM results need staff review."
        )
    else:
        st.caption("Runs locally on 140 synthetic examples. No API key or model download needed.")
    st.divider()
    st.markdown("**Current scope**")
    st.caption("Classify · Triage · Review")
    st.caption("Ticketing, RAG and autonomous agents belong to later phases.")


def service() -> Classifier:
    if backend_name == "Offline baseline":
        return Classifier(baseline(), threshold)
    return Classifier(
        OllamaBackend(
            host=host,
            model=model,
            api_key=os.getenv("OLLAMA_API_KEY", ""),
            timeout=float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "45")),
        ),
        threshold,
    )


active_settings = (backend_name, host, model, threshold)
if st.session_state.get("classification_settings") != active_settings:
    st.session_state.pop("single_result", None)
    st.session_state["classification_settings"] = active_settings


st.html('<div class="eyebrow">RESIDENT SERVICES / CLASSIFIER LAB</div>')
st.title("Every request starts here.")
st.html(
    '<p class="intro">Turn a resident message into a category, priority and suggested team. '
    "Inspect the result before it moves into an operational workflow.</p>"
)
st.caption("10 categories  ·  English starter dataset  ·  Human review built in")
single_tab, batch_tab, evaluation_tab, guide_tab, showcase_tab = st.tabs(
    [
        "Classify a request",
        "Batch review",
        "Evaluation",
        "Category guide",
        "Showcase & setup",
    ]
)

examples = {label.name: label.example for label in LABELS.values()}
examples["Emergency report"] = "My child is trapped in the lift at Block B."
examples["Multiple requests"] = "The tap in #05-12 is leaking. Also please send my payment receipt."


def load_example() -> None:
    if st.session_state.example in examples:
        st.session_state.request_text = examples[st.session_state.example]
        st.session_state.request_location = ""
        st.session_state.pop("single_result", None)


with single_tab:
    left, right = st.columns([1.05, 1], gap="large")
    with left:
        st.subheader("Resident message")
        st.selectbox(
            "Try an example",
            list(examples),
            index=None,
            placeholder="Choose a sample request",
            key="example",
            on_change=load_example,
        )
        if "request_text" not in st.session_state:
            st.session_state.request_text = examples["Maintenance"]
        with st.form("request_form"):
            text = st.text_area("Request", key="request_text", height=180, max_chars=4000)
            channel_col, location_col = st.columns(2)
            with channel_col:
                channel = st.selectbox("Channel", ["Web", "WhatsApp", "Email", "Phone transcript"])
            with location_col:
                location = st.text_input(
                    "Location (optional)",
                    key="request_location",
                    max_chars=120,
                    placeholder="Block / unit / area",
                )
            submitted = st.form_submit_button("Classify request", type="primary", width="stretch")
        st.caption("Classification only. No tickets, messages, payments or approvals are created.")
        if submitted:
            st.session_state.pop("single_result", None)
            try:
                request = ResidentRequest(text=text, channel=channel, location=location)
                with st.spinner("Classifying the request…"):
                    result = service().classify(request)
                st.session_state.single_result = result.model_dump(mode="json")
                st.session_state.classified_text = request.text
            except ValidationError:
                st.error(
                    "Enter a request of 3–4,000 characters and a location of up to 120 characters."
                )
            except ValueError as exc:
                st.error(str(exc))
    with right:
        st.subheader("Classification")
        result = st.session_state.get("single_result")
        if not result:
            with st.container(border=True):
                st.markdown("**Ready when you are.**")
                st.write(
                    "Enter a resident request or choose an example "
                    "to see its proposed category and priority."
                )
                st.caption("Ambiguous messages and urgent reports are flagged for staff review.")
        else:
            with st.container(border=True):
                if result["priority"] == "emergency":
                    st.error(
                        "Possible emergency — contact on-site security or emergency services now. "
                        "This app does not dispatch help."
                    )
                elif result["review_required"]:
                    st.warning("Staff review required")
                else:
                    st.success("Classification ready for review")
                st.markdown(f"### {LABELS[result['category']].name}")
                a, b = st.columns(2)
                a.metric("Priority", result["priority"].title())
                score = result["confidence"]
                b.metric("Model score", f"{score:.0%}" if score is not None else "—")
                st.write(f"**Suggested team:** {result['suggested_team']}")
                st.write(f"**Location:** {result['location'] or 'Not provided'}")
                for reason in result["review_reasons"]:
                    st.write(f"• {reason}")
                if result["missing_details"]:
                    st.markdown("**Details to request**")
                    for detail in result["missing_details"]:
                        st.write(f"• {detail}")
                st.caption(
                    f"{result['backend']} · {result['model']} · {result['elapsed_ms']:.0f} ms"
                )
            st.caption(
                "Scores are uncalibrated estimates, not the probability "
                "that this decision is correct."
            )
            with st.expander("Inspect the classified message and JSON"):
                st.text(st.session_state.classified_text)
                st.json(result)
            if result["category_scores"]:
                with st.expander("Compare category scores"):
                    scores = pd.DataFrame(
                        [
                            {"Category": LABELS[key].name, "Score": value}
                            for key, value in result["category_scores"].items()
                        ]
                    ).sort_values("Score", ascending=False)
                    st.bar_chart(scores, x="Category", y="Score", horizontal=True, color="#17665E")
            st.download_button(
                "Download classification JSON",
                json.dumps(result, indent=2),
                "classification.json",
                "application/json",
            )

with batch_tab:
    st.subheader("Review a small inbox")
    st.write("Upload a UTF-8 CSV with a `text` column. Optional columns: `channel`, `location`.")
    st.caption("Up to 100 requests and 2 MB. Each row is checked independently.")
    st.download_button(
        "Download sample CSV",
        (ROOT / "data/batch_example.csv").read_bytes(),
        "batch_example.csv",
        "text/csv",
    )
    upload = st.file_uploader("Request CSV", type="csv")
    if upload:
        content = upload.getvalue()
        signature = hashlib.sha256(
            content + f"{backend_name}|{host}|{model}|{threshold}".encode()
        ).hexdigest()
        try:
            rows = parse_batch(content)
            st.caption(f"{len(rows)} requests · {backend_name}")
            if st.button(f"Classify {len(rows)} requests", type="primary"):
                with st.spinner("Classifying the inbox…"):
                    batch_results = classify_batch(service(), rows)
                st.session_state.batch_result = (signature, batch_results)
            stored_batch = st.session_state.get("batch_result")
            if stored_batch and stored_batch[0] == signature:
                st.dataframe(
                    pd.DataFrame(stored_batch[1])
                    .reindex(columns=["row", "text", "category", "priority", "review_required"])
                    .map(spreadsheet_cell),
                    column_config={
                        "row": st.column_config.NumberColumn("Row", width="small"),
                        "text": st.column_config.TextColumn("Resident request", width="medium"),
                        "category": st.column_config.TextColumn("Category", width="small"),
                        "priority": st.column_config.TextColumn("Priority", width="small"),
                        "review_required": st.column_config.CheckboxColumn(
                            "Review required", width="small"
                        ),
                    },
                    hide_index=True,
                    width="stretch",
                )
                st.download_button(
                    "Download classifications CSV",
                    export_csv(stored_batch[1]),
                    "classified_requests.csv",
                    "text/csv",
                )
        except ValueError as exc:
            st.error(str(exc))

with evaluation_tab:
    st.subheader("Measure before expanding")
    st.write(
        "Evaluate the offline baseline on 55 held-out synthetic requests, "
        "including five emergencies."
    )
    st.info(
        "These examples test the starter workflow. "
        "They do not measure performance on real resident requests."
    )
    if st.button("Evaluate offline baseline"):
        with st.spinner("Evaluating the held-out examples…"):
            st.session_state.evaluation = evaluate(Classifier(baseline(), threshold))
    report = st.session_state.get("evaluation")
    if report:
        a, b, c, d = st.columns(4)
        a.metric("Category accuracy", f"{report['category_accuracy']:.1%}")
        b.metric("Macro F1", f"{report['macro_f1']:.3f}")
        c.metric("Review rate", f"{report['review_rate']:.1%}")
        d.metric("Emergency recall", f"{report['emergency_recall']:.0%}")
        st.caption(
            f"{report['rows']} synthetic examples · threshold {report['review_threshold']:.2f} · "
            f"{report['provider_errors']} provider errors · includes deterministic safety screening"
        )
        st.markdown(
            "**Confusion matrix** · Rows are expected categories; columns are predicted categories."
        )
        st.dataframe(
            pd.DataFrame(
                report["confusion_matrix"], index=report["labels"], columns=report["labels"]
            ),
            width="stretch",
        )
        misses = [
            row
            for row in report["predictions"]
            if row["category"] != row["expected_category"]
            or row["priority"] != row["expected_priority"]
        ]
        with st.expander(f"Inspect {len(misses)} category or priority mismatches"):
            st.dataframe(pd.DataFrame(misses), hide_index=True)
        st.download_button(
            "Download evaluation report",
            json.dumps(report, indent=2),
            "evaluation.json",
            "application/json",
        )

with guide_tab:
    st.subheader("One shared set of labels")
    st.write("Use these definitions when collecting and labelling real requests.")
    for key, label in LABELS.items():
        with st.expander(f"{label.name} · {label.team}"):
            st.write(label.description)
            st.caption(f"Example: {label.example}")
            st.code(key.value, language=None)
    st.info(
        "One primary category is returned. Messages with several requests need review. "
        "A booking is facilities; a broken facility is maintenance; its refund is billing."
    )

with showcase_tab:
    st.subheader("Project showcase")
    st.write(
        "This website uses the classifier in this project for every submitted request. "
        "Choose the offline model or Ollama in the sidebar, then review the suggested result."
    )
    slide_folder = ROOT / "output/slides"
    slide_images = sorted(slide_folder.glob("slide-[0-9][0-9].png"))
    if slide_images:
        slide_number = st.select_slider(
            "Showcase slide", options=list(range(1, len(slide_images) + 1)), value=1
        )
        st.image(str(slide_images[slide_number - 1]), width="stretch")
        for extension, title, mime in (
            (
                "pptx",
                "Download PowerPoint",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ),
            ("pdf", "Download slides PDF", "application/pdf"),
        ):
            deck = slide_folder / f"Management_Agent_Classifier_Showcase.{extension}"
            if deck.is_file():
                st.download_button(title, deck.read_bytes(), deck.name, mime)
    else:
        st.caption("The slide deck has not been generated in this copy of the project yet.")

    demo_video = ROOT / "output/demo/Management_Agent_Classifier_Demo.mp4"
    if demo_video.is_file():
        with st.expander("Watch the 79-second video"):
            st.video(str(demo_video))
            st.caption(
                "The video shows the original offline demo. The slides include the later "
                "local Ollama results. Historical measurements use synthetic examples."
            )
    st.subheader("Start the project on Windows")
    st.write(
        "Double-click start_project.cmd in the project folder to start the website and Ollama."
    )
    st.code(".\\start_project.cmd", language="powershell")
    st.write(
        "Open http://localhost:8501. The offline classifier is ready immediately. "
        "Select Ollama to use gemma3:4b; its first request may take longer while the model loads. "
        "Keep the launcher window open. Press Ctrl+C there to stop the website."
    )
    with st.expander("Offline startup and first-time setup"):
        st.code(
            "# First-time dependency setup, if .venv is missing\n"
            "uv sync --locked\n\n"
            "# Start the website without starting Ollama\n"
            ".\\start_project.cmd -SkipOllama",
            language="powershell",
        )
        st.caption(
            "The app runs locally. It classifies requests and exports results; "
            "ticket creation and resident messaging are future work."
        )

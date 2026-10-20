"""Compare local Ollama models as the request classifier (and chat model) on your machine.

Runs the 55-message evaluation set through each model, next to the offline baseline, and
prints one table. Needs Ollama running; no API key. With no model names it benchmarks
every model `ollama list` shows:

    uv run python scripts/benchmark_models.py
    uv run python scripts/benchmark_models.py phi4-mini gemma4:12b

Results are saved to reports/model-benchmark.json and reports/model-benchmark.md.
Accuracy doesn't depend on your hardware; latency does.
"""

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, median

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ollama  # noqa: E402

from condo_classifier.backends import BaselineBackend, OllamaBackend  # noqa: E402
from condo_classifier.chat import Conversation, OllamaChat  # noqa: E402
from condo_classifier.evaluation import evaluate  # noqa: E402
from condo_classifier.knowledge import KnowledgeBase, looks_like_question  # noqa: E402
from condo_classifier.schema import ResidentRequest  # noqa: E402
from condo_classifier.service import LOW_SCORE, Classifier  # noqa: E402
from condo_classifier.staff import review_notes  # noqa: E402

REPORTS = ROOT / "reports"
WARM_UP = "The corridor light outside #05-12 keeps flickering."
# A few chat turns to eyeball each model's replies: two questions, two requests.
CHAT_SAMPLES = [
    "what time does the gym close?",
    "can i bring my friends to the pool",
    "my kitchen sink is leaking",
    "the aircon in the function room is not working, block 3",
]


def classifier_metrics(name: str, backend, threshold: float) -> dict:
    print(f"\n== {name}", flush=True)
    started = time.perf_counter()
    try:
        backend.predict(ResidentRequest(text=WARM_UP))
    except Exception as exc:  # report and carry on with the other models
        print(f"   could not run: {exc}")
        return {"name": name, "error": str(exc)}
    cold = time.perf_counter() - started
    print(f"   loaded in {cold:.1f}s, classifying 55 messages...", flush=True)
    report = evaluate(Classifier(backend, review_threshold=threshold))
    # Emergencies are caught by rules before any model runs, so leave them out of latency.
    model_rows = [p for p in report["predictions"] if p["backend"] != "safety_rules"]
    latencies = [p["elapsed_ms"] for p in model_rows]
    return {
        "name": name,
        "category_accuracy": report["category_accuracy"],
        "macro_f1": report["macro_f1"],
        "priority_accuracy": report["priority_accuracy"],
        "emergency_recall": report["emergency_recall"],
        "invalid_outputs": report["provider_errors"],
        "low_score_rate": mean(LOW_SCORE in p["review_reasons"] for p in model_rows),
        # What staff actually see on an open card (see staff.review_notes).
        "flagged_rate": mean(bool(review_notes(p["review_reasons"], "open")) for p in model_rows),
        "load_seconds": round(cold, 1),
        "mean_ms": round(mean(latencies)),
        "median_ms": round(median(latencies)),
        "mistakes": [
            {"text": p["text"], "expected": p["expected_category"], "got": p["category"]}
            for p in report["predictions"]
            if p["expected_category"] != p["category"]
        ],
    }


def chat_samples(model: str, knowledge: KnowledgeBase) -> list[dict]:
    chat = OllamaChat.from_env()
    chat.model = model
    classifier = Classifier(BaselineBackend())
    samples = []
    for text in CHAT_SAMPLES:
        conversation = Conversation(resident_texts=[text])
        if looks_like_question(text):
            conversation.faq_facts = knowledge.search(text)
        result = classifier.classify(ResidentRequest(text=text))
        started = time.perf_counter()
        reply = chat.reply(conversation, text, result)
        samples.append(
            {
                "resident": text,
                "reply": reply,
                "words": len((reply or "").split()),
                "ms": round((time.perf_counter() - started) * 1000),
            }
        )
    return samples


def percent(value) -> str:
    return "-" if value is None else f"{value:.0%}"


def table(rows: list[dict]) -> str:
    header = (
        "| Model | Category acc. | Macro F1 | Priority acc. | Invalid output | "
        "Below threshold | Flagged for staff | Mean / median latency | Load |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
    )
    lines = []
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['name']} | could not run: {r['error']} |||||||| |")
            continue
        lines.append(
            f"| {r['name']} | {percent(r['category_accuracy'])} | {r['macro_f1']:.3f} | "
            f"{percent(r['priority_accuracy'])} | {r['invalid_outputs']}/50 | "
            f"{percent(r['low_score_rate'])} | {percent(r['flagged_rate'])} | "
            f"{r['mean_ms'] / 1000:.1f}s / {r['median_ms'] / 1000:.1f}s | {r['load_seconds']}s |"
        )
    return header + "\n".join(lines)


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("models", nargs="*", help="Ollama model names (default: all installed)")
    parser.add_argument("--no-chat", action="store_true", help="skip the chat reply samples")
    args = parser.parse_args()
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    threshold = float(os.getenv("REVIEW_THRESHOLD", "0.60"))
    models = args.models
    if not models:
        try:
            installed = ollama.Client(host=host).list().models
        except Exception as exc:
            raise SystemExit(f"Can't reach Ollama at {host}. Is it running? ({exc})") from exc
        models = [m.model for m in installed if "embed" not in m.model]
        if not models:
            raise SystemExit("No Ollama models installed. Try: ollama pull phi4-mini")
    print(f"Benchmarking: baseline, {', '.join(models)}")

    rows = [classifier_metrics("baseline (offline)", BaselineBackend(), threshold)]
    timeout = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "120"))
    knowledge = KnowledgeBase()
    chats = {}
    for model in models:
        backend = OllamaBackend(
            host, model, api_key=os.getenv("OLLAMA_API_KEY", ""), timeout=timeout
        )
        rows.append(classifier_metrics(model, backend, threshold))
        if not args.no_chat and "error" not in rows[-1]:
            chats[model] = chat_samples(model, knowledge)

    markdown = [f"# Model benchmark, {datetime.now(UTC):%Y-%m-%d}", "", table(rows), ""]
    markdown.append(
        "Invalid output: replies that failed the JSON schema or quoted text that isn't in "
        "the message, twice in a row (the request then goes to staff unclassified). "
        'Below threshold: model score under REVIEW_THRESHOLD, the "Model score is below '
        'the review threshold" note. Flagged for staff: any review note on an open card.'
    )
    for model, samples in chats.items():
        markdown += ["", f"## {model} chat replies", ""]
        for s in samples:
            markdown.append(
                f"- **{s['resident']}** → {s['reply']} _({s['words']} words, "
                f"{s['ms'] / 1000:.1f}s)_"
            )
    for r in rows:
        if r.get("mistakes"):
            markdown += ["", f"## {r['name']} mistakes", ""]
            markdown += [
                f"- {m['text']} (expected {m['expected']}, got {m['got']})" for m in r["mistakes"]
            ]
    text = "\n".join(markdown) + "\n"
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "model-benchmark.md").write_text(text, encoding="utf-8")
    (REPORTS / "model-benchmark.json").write_text(
        json.dumps({"classifier": rows, "chat": chats}, indent=2), encoding="utf-8"
    )
    print("\n" + text)
    ranked = [r for r in rows[1:] if "error" not in r]
    if ranked:
        best = max(ranked, key=lambda r: (r["macro_f1"], -r["invalid_outputs"], -r["mean_ms"]))
        print(f"Most accurate local model: {best['name']}. To use it, set in .env:")
        print(f"  CLASSIFIER_BACKEND=ollama\n  OLLAMA_MODEL={best['name']}")
    print(f"Saved {REPORTS / 'model-benchmark.md'}")


if __name__ == "__main__":
    main()

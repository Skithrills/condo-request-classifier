import argparse
import json
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from condo_classifier.backends import ROOT, BaselineBackend, OllamaBackend
from condo_classifier.evaluation import evaluate, save_report
from condo_classifier.schema import ResidentRequest
from condo_classifier.service import Classifier


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Condominium resident request classifier")
    parser.add_argument("--backend", choices=["baseline", "ollama"], default="baseline")
    sub = parser.add_subparsers(dest="command", required=True)
    classify = sub.add_parser("classify")
    classify.add_argument("text")
    classify.add_argument("--location")
    evaluation = sub.add_parser("evaluate")
    evaluation.add_argument("--output", type=Path, default=Path("reports/baseline-evaluation.json"))
    args = parser.parse_args()
    try:
        backend = BaselineBackend() if args.backend == "baseline" else OllamaBackend.from_env()
        service = Classifier(backend)
        if args.command == "classify":
            result = service.classify(ResidentRequest(text=args.text, location=args.location))
            print(result.model_dump_json(indent=2))
            return 2 if result.status == "provider_error" else 0
        report = evaluate(service)
        save_report(report, args.output)
        print(
            json.dumps(
                {
                    key: report[key]
                    for key in (
                        "rows",
                        "category_accuracy",
                        "macro_f1",
                        "priority_accuracy",
                        "review_rate",
                        "emergency_recall",
                        "provider_errors",
                    )
                },
                indent=2,
            )
        )
        print(f"Report: {args.output.resolve()}")
        return 2 if report["provider_errors"] else 0
    except (ValueError, ValidationError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())

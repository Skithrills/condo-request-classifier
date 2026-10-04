from time import perf_counter
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from condo_classifier.backends import Backend, ProviderError
from condo_classifier.schema import Category, Classification, Prediction, Priority, ResidentRequest
from condo_classifier.signals import (
    INSTRUCTION_ATTACK,
    SENSITIVE,
    SafetySignal,
    location_hint,
    multiple_topics,
    priority_hint,
    safety_signal,
)
from condo_classifier.taxonomy import LABELS


class State(TypedDict, total=False):
    request: ResidentRequest
    signal: SafetySignal | None
    prediction: Prediction
    error: str | None
    result: Classification


class Classifier:
    def __init__(self, backend: Backend, review_threshold: float = 0.60):
        if not 0 <= review_threshold <= 1:
            raise ValueError("Review threshold must be between 0 and 1.")
        self.backend = backend
        self.review_threshold = review_threshold
        graph = StateGraph(State)
        graph.add_node("safety_screen", self._screen)
        graph.add_node("classify", self._predict)
        graph.add_node("review_policy", self._review)
        graph.add_edge(START, "safety_screen")
        graph.add_conditional_edges(
            "safety_screen",
            lambda state: "review_policy" if state["signal"] else "classify",
            ["classify", "review_policy"],
        )
        graph.add_edge("classify", "review_policy")
        graph.add_edge("review_policy", END)
        self.graph = graph.compile()

    def classify(self, request: ResidentRequest) -> Classification:
        started = perf_counter()
        result = self.graph.invoke({"request": request, "error": None})["result"]
        return result.model_copy(update={"elapsed_ms": round((perf_counter() - started) * 1000, 1)})

    def _screen(self, state: State) -> dict:
        signal = safety_signal(state["request"].text)
        if not signal:
            return {"signal": None}
        return {
            "signal": signal,
            "prediction": Prediction(
                category=signal.category,
                priority=Priority.EMERGENCY,
                confidence_kind="unavailable",
                evidence=[signal.evidence],
                backend="safety_rules",
                model="english-safety-screen-v1",
            ),
        }

    def _predict(self, state: State) -> dict:
        try:
            return {"prediction": self.backend.predict(state["request"])}
        except ProviderError as exc:
            return {
                "error": str(exc),
                "prediction": Prediction(
                    category=Category.OTHER,
                    confidence_kind="unavailable",
                    backend="provider_error",
                    model="unavailable",
                ),
            }

    def _review(self, state: State) -> dict:
        request, prediction = state["request"], state["prediction"]
        location = request.location or prediction.location or location_hint(request.text)
        priority = prediction.priority
        if priority_hint(request.text) == Priority.HIGH and priority != Priority.EMERGENCY:
            priority = Priority.HIGH
        reasons, missing = [], []
        if state.get("error"):
            reasons.append(state["error"])
        if priority in {Priority.HIGH, Priority.EMERGENCY}:
            reasons.append("Urgent report requires staff triage.")
        if prediction.category == Category.OTHER:
            reasons.append("Request is unclear or outside the supported categories.")
            missing.append("A clearer description of the resident's request")
        if prediction.confidence is not None and prediction.confidence < self.review_threshold:
            reasons.append("Model score is below the review threshold.")
        if prediction.confidence_kind == "self_reported":
            reasons.append(
                "LLM confidence is not calibrated; staff must confirm the classification."
            )
        if LABELS[prediction.category].needs_location and not location:
            missing.append("Block, unit or shared-area location")
            reasons.append("Location is missing.")
        if prediction.multiple_issues or multiple_topics(request.text):
            reasons.append(
                "Possible multiple requests; staff should split or confirm the category."
            )
        if prediction.category == Category.RENOVATION or SENSITIVE.search(request.text):
            reasons.append(
                "Sensitive access, money or approval request needs an authorised person."
            )
        if INSTRUCTION_ATTACK.search(request.text):
            reasons.append(
                "Message contains instructions that may be unrelated to the resident request."
            )
        if priority == Priority.EMERGENCY:
            status = "safety_escalation"
        elif state.get("error"):
            status = "provider_error"
        else:
            status = "needs_review" if reasons else "classified"
        return {
            "result": Classification(
                category=prediction.category,
                priority=priority,
                suggested_team=LABELS[prediction.category].team,
                review_required=bool(reasons),
                review_reasons=reasons,
                missing_details=missing,
                location=location,
                confidence=prediction.confidence,
                confidence_kind=prediction.confidence_kind,
                category_scores=prediction.category_scores,
                evidence=prediction.evidence,
                backend=prediction.backend,
                model=prediction.model,
                status=status,
                elapsed_ms=0,
            )
        }

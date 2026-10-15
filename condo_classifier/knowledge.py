"""Retrieval over the building's FAQ, so the bot can answer common questions from facts.

The bot retrieves the closest FAQ entries for a resident's message and gives them to the
chat model as context (retrieval-augmented generation). Retrieval is TF-IDF over each
entry's question and keywords: local, fast and provider-neutral. Swap in embeddings later
by keeping `search` the same.
"""

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.pipeline import FeatureUnion

ROOT = Path(__file__).resolve().parents[1]
FAQ_PATH = ROOT / "data" / "condo_faq.csv"
# Chosen on data/faq_eval.csv (see tests/test_knowledge.py). Scores alone can't tell a
# question from a report ("charged twice for my maintenance fee" scores high against the
# fees entry), so the bot also requires looks_like_question(); with that gate, 0.20
# answers 33 of 36 paraphrased questions and none of the reports.
DEFAULT_THRESHOLD = 0.20

QUESTION = re.compile(
    r"\?|\b(?:what|whats|what's|when|where|which|who|how|why)\b|"
    r"^\s*(?:can|could|may|is|are|do|does|did|will|would|should)\b|"
    r"\b(?:can|could|may|should|do|does|did|is|are|will|would)\s+(?:i|we|my|our|you|the|it|there|guests?)\b|"
    r"\bwhat do i do\b|\bhow (?:do|can|much|long)\b|\bi want to (?:know|book|hold|reserve)\b",
    re.I,
)


@dataclass(frozen=True)
class Fact:
    id: str
    question: str
    answer: str


def looks_like_question(text: str) -> bool:
    """A question to answer, as opposed to a report of a problem."""
    return bool(QUESTION.search(text))


class KnowledgeBase:
    def __init__(self, path: Path = FAQ_PATH, threshold: float = DEFAULT_THRESHOLD):
        with Path(path).open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        if not rows:
            raise ValueError("The FAQ file has no entries.")
        self.facts = [Fact(r["id"], r["question"], r["answer"]) for r in rows]
        self.threshold = threshold
        self.vectorizer = FeatureUnion(
            [
                (
                    "word",
                    TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, stop_words="english"),
                ),
                (
                    "char",
                    TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True),
                ),
            ]
        )
        documents = [f"{r['question']} {r.get('keywords', '')}" for r in rows]
        self.matrix = self.vectorizer.fit_transform(documents)

    def scores(self, text: str) -> list[tuple[Fact, float]]:
        """Every entry with its similarity (0-1), best first."""
        # Each half of the union is a unit vector, so their dot products sum to at most 2.
        similarity = (self.matrix @ self.vectorizer.transform([text]).T).toarray().ravel() / 2
        ranked = sorted(zip(self.facts, similarity.tolist(), strict=True), key=lambda p: -p[1])
        return ranked

    def search(self, text: str, k: int = 2) -> list[Fact]:
        """Up to k entries that clear the threshold."""
        return [fact for fact, score in self.scores(text)[:k] if score >= self.threshold]


def knowledge_note(facts: list[Fact]) -> str:
    """Building information for the chat model, as part of the per-turn system note."""
    lines = ["Building information (from the management office's FAQ):"]
    lines.extend(f"- Q: {fact.question} A: {fact.answer}" for fact in facts)
    return "\n".join(lines)

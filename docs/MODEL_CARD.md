# Classifier model card

## Intended use

An educational, staff-facing classifier for English condominium resident requests.
Outputs are proposed labels and review flags. The tool has no ticketing credentials,
approval authority, knowledge of estate by-laws or ability to dispatch help.

## Data and method

`data/train.csv` contains 140 hand-authored synthetic messages, 14 per category.
`data/evaluation.csv` contains 55 separate synthetic messages. Neither set contains
actual residents' information. The examples were authored in the same development
exercise, so shared vocabulary and author bias make the evaluation optimistic.
An exact case-insensitive overlap check prevents direct train/test leakage but
does not rule out paraphrase overlap. Training and evaluation files have SHA-256
fingerprints in the report.

The offline model uses word unigrams/bigrams and character n-grams with logistic
regression. It requires no GPU. Its probability distribution is relative to the ten
known categories; high scores do not prove that an input belongs to this domain.
It does not provide general semantic understanding or zero-shot label definitions.

The Ollama adapter uses a fixed taxonomy prompt and Pydantic output validation.
Schema validity does not establish semantic correctness. Its confidence is
self-reported and is never used to bypass staff review. On 2026-10-03, local Gemma
was evaluated as described below. GPT-OSS, hosted providers and multilingual
behaviour remain unverified.

## Live local evaluation

Ollama 0.35.0 served `gemma3:4b` (4.3B, Q4_K_M) on an RTX 3080 10 GB, using a
4,096-token context and one request slot. The full 55-case pipeline evaluation
included 50 model requests and five rule-only emergencies. The final run returned
48 valid model responses and rejected two after bounded validation retries. Of the
50 model requests, 42 had valid, correct categories (84%); the offline baseline
classified 48/50 ordinary cases correctly (96%). All LLM results required review.

Valid warmed responses averaged 1.12 seconds, with a 1.09-second median. Including
rejected responses and retries, all 50 attempts averaged 1.32 seconds. The separate
first cold request took 86.6 seconds. These timings include the classifier workflow
and were measured on a workstation running other applications; they are not a
controlled inference benchmark.

The initial live run revealed extra quotation marks around evidence snippets.
The parser now accepts matching outer quotation marks only when stripping them
leaves an exact input span. Seven regression tests cover this normalization and
continued rejection of unsupported evidence. Labels, prompts and dataset were not
changed. This was a development rerun after a compatibility fix, not a fresh blind
test. Invalid evidence still causes a review-required provider error.

The raw final report's category accuracy is 48/55, including five rule results and
one rejected case whose fallback happens to match `other`. Do not interpret that as
48 successful model classifications; the model-success figure is 42/50 above.
See `reports/ollama-verification.json` for exact model identity and successful live
Streamlit AppTest results, and both Ollama evaluation reports for every outcome.

## Limitations

- English starter data only. Singlish, spelling errors, Chinese, Malay, Tamil,
  code-switching, images and voice inputs have not been validated.
- One primary label; splitting multiple incidents remains a staff task.
- Location extraction is a grounded hint, not an estate-directory lookup.
- The review threshold is provisional, not selected using a calibration study.
- High review coverage reflects conservative rules and a small model, not a measured
  reduction in staff workload.
- Emergency and priority patterns are incomplete and can miss or overflag incidents.
  Negation tests cover selected phrases, not full natural-language negation.
- Prompt injection checks are heuristics. Input never gets tools or permission to
  execute operations, regardless of its label.
- The app keeps results in the current Streamlit session. It does not persist
  resident cases or offer access control for a public deployment. It binds to
  localhost by default.

## From working demo to credible classifier

1. Have estate staff agree the label definitions and adjudicate ambiguous examples.
2. Collect consented, redacted real requests. Keep all messages from the same case
   together when splitting data; reserve a final time-separated test set.
3. Label category, priority, missing context and review need with two reviewers;
   resolve disagreements and include a substantial unclear/out-of-domain set.
4. Compare this baseline with an Ollama model and, if justified by the dataset,
   a small fine-tuned encoder. Keep test data out of prompts and training.
5. Calibrate probabilities on a separate split. Report macro F1, per-class recall,
   emergency false negatives, calibration, review coverage and accuracy among
   suggestions that pass the review threshold.
6. Run a staff-only shadow pilot. Measure corrections and staff time before connecting
   ticket creation. Keep operational approval rules in application code.

Chroma becomes useful when a later phase retrieves approved estate documents or
labelled examples. It is not needed to establish whether this classifier works.

## How far this goes toward Jev

The typed, bounded decision interface is practical now. Reproducing Jev's training,
general zero-shot decision capability, calibration or published speed claims is a
different research effort. This project makes no performance-equivalence claim.

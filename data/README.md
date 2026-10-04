# Data

All included messages are synthetic and hand-authored for this project.

- `train.csv`: 140 examples, 14 for each of ten categories. Used by the baseline only.
- `evaluation.csv`: 55 separate examples with category and priority labels. Never trained on.
- `batch_example.csv`: four demo requests for the upload workflow.

CSV messages that contain commas must be quoted. Label keys must match
`condo_classifier/schema.py`. Real data should be redacted and kept under
`data/private/` (ignored by Git), with separate train/calibration/test partitions.
Do not add real resident names, unit histories, messages or API keys to a shared
repository. Synthetic scores do not establish production performance.

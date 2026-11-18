# question-papers-vault-moodle

## Moodle question bank categories

The `question_bank_categories` directory contains category-only Moodle XML files for
NEET (UG), JEE Main, JEE Advanced, and WBJEE. NEET (UG) and JEE Main use
English, Bengali, and Hindi; JEE Advanced uses English and Hindi; WBJEE uses
English and Bengali. NEET phase folders are labeled as Phase I or Phase II.
JEE Advanced and WBJEE categories include each paper's filename (without its
extension) between the year and language. JEE Main categories include the exam
date and available shift (Shift 1 or Shift 2) between the year and language.
The files create categories under `$module$/top/Previous Year Question Papers`
and do not include question content.

## Migrating existing extraction output

To re-home checkpointed XML outputs after reorganizing
`question-papers-vault`, preview the migration first:

```bash
python scripts/migrate_existing_extraction_outputs.py \
  --source-root ../question-papers-vault \
  --output-root .
```

Apply it only after reviewing the preview:

```bash
python scripts/migrate_existing_extraction_outputs.py \
  --source-root ../question-papers-vault \
  --output-root . \
  --apply
```

The script matches source PDFs by SHA-256, copies completed checkpointed
outputs and batch data to the new source-relative paths, updates their Moodle
category markers, and rebuilds combined XML. Original files remain in their
old locations. Outputs changed after their checkpoint are skipped by default.

If you have reviewed a changed XML and want to adopt its current contents, you
can explicitly select its checkpoint manifests. Preview first:

```bash
python scripts/migrate_existing_extraction_outputs.py \
  --source-root ../question-papers-vault \
  --output-root . \
  --accept-changed-checkpoints \
  neet_ug/2015_PII/NEET_UG_2015_Phase_II_neet_ug_english_hindi.extract.json \
  neet_ug/2016_PI/NEET_UG_2016_Phase_I_neet_ug_english_bengali.extract.json \
  neet_ug/2016_PI/NEET_UG_2016_Phase_I_neet_ug_english_hindi.extract.json
```

Add `--apply` to perform the selected migration. The migrated XML receives the
new category and the migrated checkpoint hash is updated to match that XML.
Original XML and checkpoint files remain unchanged, so use this only when the
current XML contents are authoritative.

Pre-checkpoint NEET UG XMLs can be migrated separately. Preview all legacy
years first:

```bash
python scripts/migrate_existing_extraction_outputs.py \
  --source-root ../question-papers-vault \
  --output-root . \
  --legacy-years all
```

Add `--apply` after reviewing the preview. This mode matches legacy filenames
to source PDF stems, updates category markers, and rebuilds combined XML. It
copies only question-bearing language outputs; category-only placeholder XMLs
and outputs already migrated are skipped. Original files remain in their old
locations, and no extraction checkpoint is created, so these legacy outputs
cannot be continued with `extract --resume`. Use specific four-digit years
instead of `all` to limit the migration. The checkpointed migration remains
the default when `--legacy-years` is omitted. Continue checkpointed runs with
the same extraction settings (including `--mode agent` for checkpoints created
by the agent backend).

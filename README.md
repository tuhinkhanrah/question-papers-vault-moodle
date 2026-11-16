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

## NCERT Exemplar combined imports

From the repository root, rebuild one import per class and subject:

```powershell
python scripts\combine_ncert_exemplar_xml.py --root neet_ug\ncert_exemplar
```

By default, the script expects English, Bengali and Hindi chapter XMLs and writes
the six combined files into their respective class and subject folders. Use
`--class class11` and/or `--subject biology` to select a subset. Use
`--languages english,bengali,hindi,tamil` when adding another language, and
`--output-root` to write the combined files elsewhere. Missing or unexpected
language files stop the build; empty files produce a warning.

Each chapter/language gets a Moodle category. If a source XML already has a
category marker, the combiner preserves it; otherwise it constructs one under
`$module$/top/NCERT Exemplar Question Bank`, or the supplied `--category-root`.
Enable **Get category from file** when importing. Combining files does not
check question content or source coverage.
Re-importing into an existing Moodle bank can create duplicates.
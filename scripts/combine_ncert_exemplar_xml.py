#!/usr/bin/env python3
"""Combine NCERT Exemplar Moodle XML files by class and subject."""

import argparse
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


CLASSES = ("class11", "class12")
SUBJECTS = ("biology", "chemistry", "physics")
DEFAULT_CATEGORY_ROOT = "$module$/top/NCERT Exemplar Question Bank"


def parse_languages(value: str) -> list[str]:
    languages = [part.strip().casefold() for part in value.split(",")]
    if not languages or any(not re.fullmatch(r"[a-z]+(?:_[a-z]+)*", part) for part in languages):
        raise ValueError("Languages must be comma-separated ASCII names, using underscores for spaces.")
    if len(set(languages)) != len(languages):
        raise ValueError("Languages must not contain duplicates.")
    return languages


def build_combined(
    folder: Path,
    class_name: str,
    subject: str,
    languages: list[str],
    category_root: str,
) -> tuple[ET.Element, int, int, list[Path]]:
    pattern = re.compile(r"^(.+)_neet_ug_english(?:_([a-z]+(?:_[a-z]+)*))?\.xml$", re.IGNORECASE)
    chapters: dict[str, dict[str, Path]] = {}
    for path in sorted(folder.glob("*.xml")):
        match = pattern.fullmatch(path.name)
        if not match:
            continue
        chapter = match.group(1)
        language = (match.group(2) or "english").casefold()
        if language not in languages:
            raise ValueError(f"{path}: language {language!r} is not listed in --languages.")
        files = chapters.setdefault(chapter, {})
        if language in files:
            raise ValueError(f"Duplicate {language} XML for chapter {chapter}: {path}")
        files[language] = path
    if not chapters:
        raise ValueError(f"No chapter language XML files found in {folder}.")

    quiz = ET.Element("quiz")
    question_count = 0
    empty_files = []
    for chapter, files in sorted(chapters.items()):
        missing = [language for language in languages if language not in files]
        if missing:
            raise ValueError(f"{folder / chapter}: missing XML for {', '.join(missing)}.")
        for language in languages:
            path = files[language]
            try:
                source = ET.parse(path).getroot()
            except (OSError, ET.ParseError) as exc:
                raise ValueError(f"Cannot read Moodle XML {path}: {exc}") from exc
            if source.tag != "quiz" or any(
                node.tag != "question" or not node.get("type") for node in source
            ):
                raise ValueError(f"{path}: expected a quiz containing typed questions.")
            markers = [node for node in source if node.get("type") == "category"]
            if markers:
                if (len(markers) != 1 or source[0] is not markers[0]
                        or not (markers[0].findtext("category/text") or "").strip()):
                    raise ValueError(f"{path}: expected one non-empty category before the questions.")
                quiz.append(markers[0])
            else:
                label = language.replace("_", " ").title()
                category_path = (
                    f"{category_root.rstrip('/')}/Class {class_name[5:]}/"
                    f"{subject.title()}/{chapter}/{label}"
                )
                category = ET.SubElement(quiz, "question", {"type": "category"})
                ET.SubElement(ET.SubElement(category, "category"), "text").text = category_path
            questions = [node for node in source if node.get("type") != "category"]
            if not questions:
                empty_files.append(path)
            for question in questions:
                quiz.append(question)
                question_count += 1
    return quiz, len(chapters) * len(languages), question_count, empty_files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="NCERT Exemplar source XML root.")
    parser.add_argument("--output-root", type=Path, help="Output root; defaults to the source root.")
    parser.add_argument("--class", dest="class_name", choices=CLASSES, help="Combine one class.")
    parser.add_argument("--subject", choices=SUBJECTS, help="Combine one subject.")
    parser.add_argument("--languages", default="english,bengali,hindi", help="Expected languages, comma-separated.")
    parser.add_argument("--category-root", default=DEFAULT_CATEGORY_ROOT, help="Moodle category path prefix.")
    args = parser.parse_args(argv)
    try:
        languages = parse_languages(args.languages)
        if not args.category_root.strip().strip("/"):
            raise ValueError("Category root must not be empty.")
        root = args.root.resolve()
        if not root.is_dir():
            raise ValueError(f"Source root is not a directory: {root}")
        output_root = (args.output_root or root).resolve()
        classes = (args.class_name,) if args.class_name else CLASSES
        subjects = (args.subject,) if args.subject else SUBJECTS
        results = []
        for class_name in classes:
            for subject in subjects:
                quiz, categories, questions, empty = build_combined(
                    root / class_name / subject, class_name, subject, languages, args.category_root.strip()
                )
                output = output_root / class_name / subject / f"{class_name}_{subject}_combined_moodle.xml"
                results.append((output, quiz, categories, questions, empty))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    for output, quiz, categories, questions, empty in results:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".tmp", delete=False) as handle:
                temporary_path = Path(handle.name)
                ET.ElementTree(quiz).write(handle, encoding="utf-8", xml_declaration=True)
            os.replace(temporary_path, output)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        print(f"{output}: {categories} categories, {questions} questions.")
        for path in empty:
            print(f"WARNING: {path} has no questions; review the source.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

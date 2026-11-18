#!/usr/bin/env python3
"""Copy Moodle extraction outputs into the current source layout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET


SOURCE_CATEGORIES = {
    "ncert-exemplar-question-bank": "NCERT Exemplar Question Bank",
    "previous-year-question-bank": "Previous Year Question Bank",
}
LEGACY_OUTPUT_ROOTS = ("ncert_exemplar", "neet_ug")
LEGACY_LANGUAGE_SUFFIX = re.compile(
    r"_neet_ug_(english(?:_(?:bengali|hindi))?)$",
    re.IGNORECASE,
)
LEGACY_YEAR = re.compile(r"^(\d{4})")


@dataclass(frozen=True)
class MigrationItem:
    source_pdf: Path
    old_manifest: Path
    old_output: Path
    target_output: Path
    target_manifest: Path
    updated_manifest: dict[str, Any]
    category_path: str
    old_batch_dir: Path | None
    target_batch_dir: Path | None
    old_work_dir: Path | None
    target_work_dir: Path | None


@dataclass
class MigrationPlan:
    items: list[MigrationItem]
    skipped: list[str]


@dataclass(frozen=True)
class LegacyXMLMigrationItem:
    source_pdf: Path
    old_output: Path
    target_output: Path
    category_path: str


@dataclass
class LegacyXMLMigrationPlan:
    items: list[LegacyXMLMigrationItem]
    skipped: list[str]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_pipeline_helpers(pipeline_root: Path):
    scripts_dir = pipeline_root / "scripts"
    source_dir = pipeline_root / "src"
    if not scripts_dir.is_dir() or not source_dir.is_dir():
        raise ValueError(f"Academic content pipeline not found at {pipeline_root}")
    for path in (str(scripts_dir), str(source_dir)):
        if path not in sys.path:
            sys.path.insert(0, path)
    from run_path_mirrored_extraction import (
        generated_output_path,
        infer_metadata_from_path,
    )
    from academic_content_pipeline.moodle_categories import (
        build_category_path,
        set_moodle_category,
    )

    return generated_output_path, infer_metadata_from_path, build_category_path, set_moodle_category


def build_migration_plan(
    source_root: Path,
    output_root: Path,
    pipeline_root: Path,
    *,
    selected_manifests: set[str] | None = None,
    accepted_changed_manifests: set[str] | None = None,
    allow_existing_work_dirs: bool = False,
) -> MigrationPlan:
    source_root = source_root.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    generated_output_path, infer_metadata_from_path, build_category_path, _ = (
        _load_pipeline_helpers(pipeline_root.expanduser().resolve())
    )
    source_by_hash: dict[str, list[Path]] = {}
    for pdf_path in sorted(source_root.rglob("*")):
        if pdf_path.is_file() and not pdf_path.is_symlink() and pdf_path.suffix.casefold() == ".pdf":
            source_by_hash.setdefault(_sha256_file(pdf_path), []).append(pdf_path)

    manifest_paths = [
        manifest
        for old_root in LEGACY_OUTPUT_ROOTS
        for manifest in sorted((output_root / old_root).rglob("*.extract.json"))
    ]
    manifest_paths_by_name = {
        manifest.relative_to(output_root).as_posix(): manifest
        for manifest in manifest_paths
    }
    if selected_manifests is not None:
        missing_manifests = selected_manifests - manifest_paths_by_name.keys()
        if missing_manifests:
            missing = ", ".join(sorted(missing_manifests))
            raise ValueError(f"Selected extraction checkpoints were not found: {missing}")
        manifest_paths = [
            manifest_paths_by_name[name]
            for name in sorted(selected_manifests)
        ]
    if not manifest_paths:
        raise ValueError("No extraction checkpoints found under the legacy output roots.")
    if accepted_changed_manifests is not None and (
        selected_manifests is None
        or not accepted_changed_manifests.issubset(selected_manifests)
    ):
        raise ValueError("Changed XML acceptance requires selecting those manifests explicitly.")

    items: list[MigrationItem] = []
    skipped: list[str] = []
    reserved: dict[Path, Path] = {}
    for manifest_path in manifest_paths:
        if manifest_path.is_symlink():
            skipped.append(f"{manifest_path}: checkpoint is a symlink")
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            skipped.append(f"{manifest_path}: cannot read checkpoint ({exc})")
            continue
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            skipped.append(f"{manifest_path}: unsupported checkpoint schema")
            continue
        if manifest.get("status") != "completed":
            skipped.append(f"{manifest_path}: checkpoint is not completed")
            continue

        source_hash = manifest.get("source_sha256")
        candidates = source_by_hash.get(source_hash, [])
        if len(candidates) > 1:
            old_source_name = Path(str(manifest.get("source_pdf", ""))).name.casefold()
            candidates = [candidate for candidate in candidates if candidate.name.casefold() == old_source_name]
        if len(candidates) != 1:
            skipped.append(
                f"{manifest_path}: source PDF hash matched {len(candidates)} current source files"
            )
            continue
        source_pdf = candidates[0]

        output_name = manifest.get("output_file")
        if not isinstance(output_name, str) or not output_name or Path(output_name).name != output_name:
            skipped.append(f"{manifest_path}: invalid output filename")
            continue
        old_output = manifest_path.parent / output_name
        if old_output.is_symlink() or not old_output.is_file():
            skipped.append(f"{manifest_path}: checkpoint output is missing or is a symlink")
            continue
        manifest_name = manifest_path.relative_to(output_root).as_posix()
        if (
            _sha256_file(old_output) != manifest.get("final_sha256")
            and manifest_name not in (accepted_changed_manifests or set())
        ):
            skipped.append(f"{manifest_path}: output changed since its checkpoint")
            continue

        configuration = manifest.get("configuration")
        if not isinstance(configuration, dict):
            skipped.append(f"{manifest_path}: checkpoint has no valid configuration")
            continue
        languages = configuration.get("languages")
        if (
            not isinstance(languages, list)
            or not languages
            or not all(isinstance(language, str) for language in languages)
        ):
            skipped.append(f"{manifest_path}: invalid language configuration")
            continue
        standard, tags = infer_metadata_from_path(source_pdf, source_root)
        if configuration.get("standards") != standard or configuration.get("tags", "") != ",".join(tags):
            skipped.append(f"{manifest_path}: inferred standard or tags differ from its checkpoint")
            continue

        relative_source = source_pdf.relative_to(source_root)
        top_category = SOURCE_CATEGORIES.get(relative_source.parts[0])
        if top_category is None:
            skipped.append(f"{manifest_path}: unsupported source category {relative_source.parts[0]!r}")
            continue
        language_string = ",".join(languages)
        output_stem = generated_output_path(
            Path(), source_pdf.stem, standard, language_string
        ).stem
        target_output = output_root / relative_source.parent / f"{output_stem}.xml"
        category_path = build_category_path(top_category, source_root, source_pdf, languages)

        updated_manifest = dict(manifest)
        updated_configuration = dict(configuration)
        updated_configuration["category_path"] = category_path
        updated_manifest["configuration"] = updated_configuration
        updated_manifest["source_pdf"] = source_pdf.name
        updated_manifest["output_file"] = target_output.name

        old_batch_dir = manifest_path.parent / (
            "." + manifest_path.name[: -len(".extract.json")] + ".extract_batches"
        )
        if old_batch_dir.is_symlink():
            skipped.append(f"{manifest_path}: batch directory is a symlink")
            continue
        if old_batch_dir.exists() and not old_batch_dir.is_dir():
            skipped.append(f"{manifest_path}: batch path is not a directory")
            continue
        target_batch_dir = (
            target_output.parent / ("." + output_stem + ".extract_batches")
            if old_batch_dir.is_dir()
            else None
        )

        old_source_name = Path(str(manifest.get("source_pdf", ""))).name
        old_work_dir = manifest_path.parent / Path(old_source_name).stem
        if old_work_dir.is_symlink():
            skipped.append(f"{manifest_path}: per-paper work directory is a symlink")
            continue
        if old_work_dir.exists() and not old_work_dir.is_dir():
            skipped.append(f"{manifest_path}: per-paper work path is not a directory")
            continue
        target_work_dir = (
            target_output.parent / source_pdf.stem if old_work_dir.is_dir() else None
        )

        item = MigrationItem(
            source_pdf=source_pdf,
            old_manifest=manifest_path,
            old_output=old_output,
            target_output=target_output,
            target_manifest=target_output.with_suffix(".extract.json"),
            updated_manifest=updated_manifest,
            category_path=category_path,
            old_batch_dir=old_batch_dir if old_batch_dir.is_dir() else None,
            target_batch_dir=target_batch_dir,
            old_work_dir=old_work_dir if old_work_dir.is_dir() else None,
            target_work_dir=target_work_dir,
        )
        paths = [item.target_output, item.target_manifest]
        if item.target_batch_dir is not None:
            paths.append(item.target_batch_dir)
        if item.target_work_dir is not None:
            paths.append(item.target_work_dir)
        collision = next(
            (
                path
                for path in paths
                if not (
                    allow_existing_work_dirs
                    and path == item.target_work_dir
                    and path.is_dir()
                    and not path.is_symlink()
                )
                and (
                    path.exists()
                or (
                    path in reserved
                    and not (
                        item.target_work_dir == path
                        and reserved[path] == item.source_pdf
                    )
                )
                )
            ),
            None,
        )
        if collision is not None:
            skipped.append(f"{manifest_path}: destination already exists or is duplicated: {collision}")
            continue
        for path in paths:
            reserved[path] = item.source_pdf
        items.append(item)

    return MigrationPlan(items=items, skipped=skipped)


def _normalized_stem(stem: str) -> str:
    return re.sub(r"[^a-z0-9]", "", stem.casefold())


def build_legacy_xml_migration_plan(
    source_root: Path,
    output_root: Path,
    pipeline_root: Path,
    years: set[str] | None,
) -> LegacyXMLMigrationPlan:
    source_root = source_root.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    pipeline_root = pipeline_root.expanduser().resolve()
    if not source_root.is_dir():
        raise NotADirectoryError(f"Source root does not exist: {source_root}")
    if not output_root.is_dir():
        raise NotADirectoryError(f"Output root does not exist: {output_root}")
    if years is not None and (
        not years or any(re.fullmatch(r"\d{4}", year) is None for year in years)
    ):
        raise ValueError("Legacy XML migration years must be four-digit years.")

    generated_output_path, infer_metadata_from_path, build_category_path, _ = (
        _load_pipeline_helpers(pipeline_root)
    )
    source_category_root = source_root / "previous-year-question-bank" / "neet-ug"
    legacy_output_root = output_root / "neet_ug"
    if not source_category_root.is_dir():
        raise NotADirectoryError(f"NEET UG source root does not exist: {source_category_root}")
    if not legacy_output_root.is_dir():
        raise NotADirectoryError(f"Legacy NEET UG output root does not exist: {legacy_output_root}")

    source_by_stem: dict[str, list[Path]] = {}
    for source_pdf in sorted(source_category_root.rglob("*")):
        if not source_pdf.is_file() or source_pdf.is_symlink() or source_pdf.suffix.casefold() != ".pdf":
            continue
        relative_source = source_pdf.relative_to(source_category_root)
        source_year = LEGACY_YEAR.match(relative_source.parts[0]) if relative_source.parts else None
        if source_year is None:
            continue
        if years is not None and source_year.group(1) not in years:
            continue
        source_by_stem.setdefault(_normalized_stem(source_pdf.stem), []).append(source_pdf)

    items: list[LegacyXMLMigrationItem] = []
    skipped: list[str] = []
    reserved: dict[Path, Path] = {}
    for old_output in sorted(legacy_output_root.rglob("*.xml")):
        relative_output = old_output.relative_to(legacy_output_root)
        if not relative_output.parts or any(
            part.startswith(".") for part in relative_output.parts[:-1]
        ):
            continue
        output_year = LEGACY_YEAR.match(relative_output.parts[0])
        if output_year is None or (
            years is not None and output_year.group(1) not in years
        ):
            continue
        if old_output.is_symlink():
            skipped.append(f"{old_output}: XML is a symlink")
            continue
        if old_output.with_suffix(".extract.json").exists():
            skipped.append(f"{old_output}: checkpoint exists; use the checkpointed migration")
            continue
        try:
            xml_root = ET.parse(old_output).getroot()
        except (OSError, ET.ParseError) as exc:
            skipped.append(f"{old_output}: cannot parse legacy XML ({exc})")
            continue
        if xml_root.tag != "quiz" or not any(
            question.tag == "question" and question.get("type") != "category"
            for question in xml_root
        ):
            skipped.append(f"{old_output}: XML contains no question content")
            continue

        language_match = LEGACY_LANGUAGE_SUFFIX.search(old_output.stem)
        if language_match is None:
            skipped.append(f"{old_output}: legacy filename has no supported NEET UG language suffix")
            continue

        legacy_source_stem = old_output.stem[: language_match.start()]
        source_candidates = source_by_stem.get(_normalized_stem(legacy_source_stem), [])
        source_candidates = [
            source_pdf
            for source_pdf in source_candidates
            if LEGACY_YEAR.match(
                source_pdf.relative_to(source_category_root).parts[0]
            ).group(1)
            == output_year.group(1)
        ]
        if len(source_candidates) != 1:
            skipped.append(
                f"{old_output}: filename matched {len(source_candidates)} source PDFs in "
                f"{relative_output.parts[0]}"
            )
            continue
        source_pdf = source_candidates[0]

        languages = language_match.group(1).casefold().split("_")
        standard, _ = infer_metadata_from_path(source_pdf, source_root)
        relative_source = source_pdf.relative_to(source_root)
        top_category = SOURCE_CATEGORIES.get(relative_source.parts[0])
        if top_category is None:
            skipped.append(f"{old_output}: unsupported source category {relative_source.parts[0]!r}")
            continue
        output_stem = generated_output_path(
            Path(),
            source_pdf.stem,
            standard,
            ",".join(languages),
        ).stem
        target_output = output_root / relative_source.parent / f"{output_stem}.xml"
        target_manifest = target_output.with_suffix(".extract.json")
        collision = next(
            (
                path
                for path in (target_output, target_manifest)
                if path.exists() or path in reserved
            ),
            None,
        )
        if collision is not None:
            skipped.append(f"{old_output}: destination already exists or is duplicated: {collision}")
            continue

        category_path = build_category_path(
            top_category,
            source_root,
            source_pdf,
            languages,
        )
        item = LegacyXMLMigrationItem(
            source_pdf=source_pdf,
            old_output=old_output,
            target_output=target_output,
            category_path=category_path,
        )
        reserved[target_output] = source_pdf
        items.append(item)

    return LegacyXMLMigrationPlan(items=items, skipped=skipped)


def _rebuild_combined_outputs(staging_root: Path, source_roots: set[str], pipeline_root: Path) -> None:
    _, _, _, _ = _load_pipeline_helpers(pipeline_root)
    from academic_content_pipeline.moodle_combiner import rebuild_combined_xml

    for name in sorted(source_roots):
        rebuild_combined_xml(staging_root / name)


def migrate_legacy_xml_outputs(
    source_root: Path,
    output_root: Path,
    pipeline_root: Path,
    *,
    years: set[str],
    apply: bool = False,
    rebuild_combined: bool = True,
) -> LegacyXMLMigrationPlan:
    source_root = source_root.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    pipeline_root = pipeline_root.expanduser().resolve()
    plan = build_legacy_xml_migration_plan(
        source_root,
        output_root,
        pipeline_root,
        years,
    )
    if not plan.items or not apply:
        return plan

    _, _, _, set_moodle_category = _load_pipeline_helpers(pipeline_root)
    staged_outputs: list[tuple[Path, bytes]] = []
    for item in plan.items:
        if item.target_output.exists() or item.target_output.with_suffix(".extract.json").exists():
            raise FileExistsError(
                f"Destination appeared during migration; refusing to overwrite: {item.target_output}"
            )
        xml = set_moodle_category(
            item.old_output.read_text(encoding="utf-8"),
            item.category_path,
        )
        staged_outputs.append((item.target_output, xml.encode("utf-8")))

    published: list[Path] = []
    try:
        for target_output, xml_bytes in staged_outputs:
            target_output.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=target_output.parent,
                    prefix=".legacy-xml-migration-",
                    delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    handle.write(xml_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target_output)
                published.append(target_output)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

        if rebuild_combined:
            _rebuild_combined_outputs(
                output_root,
                {"previous-year-question-bank"},
                pipeline_root,
            )
    except Exception:
        for target_output in reversed(published):
            target_output.unlink(missing_ok=True)
        raise
    return plan


def migrate_outputs(
    source_root: Path,
    output_root: Path,
    pipeline_root: Path,
    *,
    apply: bool = False,
    rebuild_combined: bool = True,
    selected_manifests: set[str] | None = None,
    accepted_changed_manifests: set[str] | None = None,
    merge_existing: bool = False,
) -> MigrationPlan:
    source_root = source_root.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    pipeline_root = pipeline_root.expanduser().resolve()
    if not source_root.is_dir():
        raise NotADirectoryError(f"Source root does not exist: {source_root}")
    if not output_root.is_dir():
        raise NotADirectoryError(f"Output root does not exist: {output_root}")

    if merge_existing and selected_manifests is None:
        raise ValueError("Merging into an existing output requires selecting manifests explicitly.")
    if accepted_changed_manifests and not merge_existing:
        raise ValueError("Accepting changed XML requires the selected merge mode.")

    plan = build_migration_plan(
        source_root,
        output_root,
        pipeline_root,
        selected_manifests=selected_manifests,
        accepted_changed_manifests=accepted_changed_manifests,
        allow_existing_work_dirs=merge_existing,
    )
    source_roots = {item.target_output.relative_to(output_root).parts[0] for item in plan.items}
    if not merge_existing:
        for name in source_roots:
            if (output_root / name).exists():
                raise FileExistsError(
                    f"New-layout output already exists; refusing to merge: {output_root / name}"
                )
    if not plan.items:
        return plan
    if not apply:
        return plan

    staging_root = Path(
        tempfile.mkdtemp(prefix=".question-papers-vault-migration-", dir=output_root.parent)
    )
    published: list[tuple[Path, Path]] = []
    try:
        _, _, _, set_moodle_category = _load_pipeline_helpers(pipeline_root)
        copied_work_dirs: set[tuple[Path, Path]] = set()
        for item in plan.items:
            staged_output = staging_root / item.target_output.relative_to(output_root)
            staged_output.parent.mkdir(parents=True, exist_ok=True)
            xml = set_moodle_category(
                item.old_output.read_text(encoding="utf-8"),
                item.category_path,
            )
            xml_bytes = xml.encode("utf-8")
            staged_output.write_bytes(xml_bytes)

            updated_manifest = dict(item.updated_manifest)
            updated_manifest["final_sha256"] = hashlib.sha256(xml_bytes).hexdigest()
            staged_manifest = staged_output.with_suffix(".extract.json")
            staged_manifest.write_text(
                json.dumps(updated_manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            if item.old_batch_dir is not None and item.target_batch_dir is not None:
                staged_batch = staging_root / item.target_batch_dir.relative_to(output_root)
                staged_batch.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(item.old_batch_dir, staged_batch)

            if item.old_work_dir is not None and item.target_work_dir is not None:
                work_key = (item.old_work_dir, item.target_work_dir)
                if work_key not in copied_work_dirs:
                    if item.target_work_dir.exists():
                        if item.target_work_dir.is_symlink() or not item.target_work_dir.is_dir():
                            raise FileExistsError(
                                f"Refusing to overwrite an existing work path: {item.target_work_dir}"
                            )
                        copied_work_dirs.add(work_key)
                        continue
                    staged_work_dir = staging_root / item.target_work_dir.relative_to(output_root)
                    staged_work_dir.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(item.old_work_dir, staged_work_dir)
                    copied_work_dirs.add(work_key)

        if rebuild_combined and not merge_existing:
            _rebuild_combined_outputs(staging_root, source_roots, pipeline_root)

        if merge_existing:
            published_work_dirs: set[tuple[Path, Path]] = set()
            for item in plan.items:
                paths = [
                    (
                        staging_root / item.target_output.relative_to(output_root),
                        item.target_output,
                    ),
                    (
                        staging_root / item.target_manifest.relative_to(output_root),
                        item.target_manifest,
                    ),
                ]
                if item.target_batch_dir is not None:
                    paths.append(
                        (
                            staging_root / item.target_batch_dir.relative_to(output_root),
                            item.target_batch_dir,
                        )
                    )
                if item.target_work_dir is not None:
                    work_key = (item.old_work_dir, item.target_work_dir)
                    staged_work_dir = staging_root / item.target_work_dir.relative_to(output_root)
                    if staged_work_dir.exists() and work_key not in published_work_dirs:
                        paths.append((staged_work_dir, item.target_work_dir))
                        published_work_dirs.add(work_key)
                for staged_path, target_path in paths:
                    if target_path.exists():
                        raise FileExistsError(
                            f"Destination appeared during migration; refusing to overwrite: {target_path}"
                        )
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    if staged_path.is_dir():
                        try:
                            shutil.copytree(staged_path, target_path)
                        except Exception:
                            if target_path.exists():
                                shutil.rmtree(target_path)
                            raise
                    else:
                        os.replace(staged_path, target_path)
                    published.append((target_path, staged_path))

            if rebuild_combined:
                _rebuild_combined_outputs(output_root, source_roots, pipeline_root)
        else:
            for name in sorted(source_roots):
                staged_root = staging_root / name
                target_root = output_root / name
                if target_root.exists():
                    raise FileExistsError(
                        f"Destination appeared during migration; refusing to overwrite: {target_root}"
                    )
                os.replace(staged_root, target_root)
                published.append((target_root, staged_root))
    except Exception:
        for target_path, staged_path in reversed(published):
            if target_path.is_dir() and not target_path.is_symlink():
                shutil.rmtree(target_path)
            elif target_path.exists() and not staged_path.exists():
                staged_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target_path, staged_path)
        if merge_existing and rebuild_combined and published:
            _rebuild_combined_outputs(output_root, source_roots, pipeline_root)
        raise
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root)
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True, help="Reorganized source PDF vault.")
    parser.add_argument("--output-root", type=Path, required=True, help="Existing Moodle output mirror.")
    parser.add_argument(
        "--pipeline-root",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "academic-content-pipeline",
        help="Sibling academic-content-pipeline checkout.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the selected migration. Without this flag, only preview.",
    )
    parser.add_argument(
        "--legacy-years",
        nargs="+",
        metavar="YEAR",
        help="Migrate checkpoint-free NEET UG XML outputs for these years, or use 'all'.",
    )
    parser.add_argument(
        "--accept-changed-checkpoints",
        nargs="+",
        metavar="MANIFEST",
        help=(
            "Explicitly accept current XML for these legacy checkpoint manifests and "
            "update the migrated manifest hash."
        ),
    )
    args = parser.parse_args(argv)
    try:
        if args.accept_changed_checkpoints:
            if args.legacy_years:
                raise ValueError(
                    "--accept-changed-checkpoints cannot be combined with --legacy-years."
                )
            selected_manifests = {
                Path(manifest).as_posix()
                for manifest in args.accept_changed_checkpoints
            }
            plan = migrate_outputs(
                args.source_root,
                args.output_root,
                args.pipeline_root,
                apply=args.apply,
                selected_manifests=selected_manifests,
                accepted_changed_manifests=selected_manifests,
                merge_existing=True,
            )
        elif args.legacy_years:
            legacy_years = set(args.legacy_years)
            if "all" in legacy_years:
                if legacy_years != {"all"}:
                    raise ValueError("'all' cannot be combined with specific legacy years.")
                legacy_years = None
            plan = migrate_legacy_xml_outputs(
                args.source_root,
                args.output_root,
                args.pipeline_root,
                years=legacy_years,
                apply=args.apply,
            )
        else:
            plan = migrate_outputs(
                args.source_root,
                args.output_root,
                args.pipeline_root,
                apply=args.apply,
            )
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.legacy_years:
        print(f"Eligible checkpoint-free legacy XML outputs: {len(plan.items)}")
        print(f"Skipped legacy XML outputs: {len(plan.skipped)}")
        for item in plan.items:
            relative_output = item.target_output.relative_to(args.output_root.resolve())
            print(f"{'Migrated' if args.apply else 'Would migrate'} {item.old_output} -> {relative_output}")
        for reason in plan.skipped:
            print(f"SKIPPED: {reason}", file=sys.stderr)
        if args.apply:
            print("Original XML files were preserved; no extraction checkpoints were created.")
        else:
            print("Dry run only. Re-run with --apply to copy the eligible XML files.")
        return 0

    print(f"Eligible checkpointed outputs: {len(plan.items)}")
    print(f"Skipped checkpoints: {len(plan.skipped)}")
    for item in plan.items:
        relative_output = item.target_output.relative_to(args.output_root.resolve())
        print(f"{'Migrated' if args.apply else 'Would migrate'} {item.old_manifest} -> {relative_output}")
    for reason in plan.skipped:
        print(f"SKIPPED: {reason}", file=sys.stderr)
    if args.apply:
        print("Original output files were preserved; no extraction was run.")
        if args.accept_changed_checkpoints:
            print("Accepted XML contents were recorded in the migrated checkpoints.")
    else:
        print("Dry run only. Re-run with --apply to copy the eligible outputs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

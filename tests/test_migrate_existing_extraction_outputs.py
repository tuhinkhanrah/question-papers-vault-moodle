import hashlib
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from migrate_existing_extraction_outputs import (
    migrate_legacy_xml_outputs,
    migrate_outputs,
)


class ExistingExtractionMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.source_root = root / "question-papers-vault"
        self.output_root = root / "question-papers-vault-moodle"
        self.pipeline_root = Path(__file__).resolve().parents[2] / "academic-content-pipeline"
        self.source_pdf = (
            self.source_root
            / "ncert-exemplar-question-bank"
            / "class11"
            / "biology"
            / "keep401.pdf"
        )
        self.old_output_dir = self.output_root / "ncert_exemplar" / "class11" / "biology"
        self.old_stem = "keep401_neet_ug_english"
        self.old_xml = self.old_output_dir / f"{self.old_stem}.xml"
        self.old_manifest = self.old_output_dir / f"{self.old_stem}.extract.json"
        self.old_batch_dir = self.old_output_dir / f".{self.old_stem}.extract_batches"
        self.old_work_dir = self.old_output_dir / "keep401"

        self.source_pdf.parent.mkdir(parents=True)
        self.source_pdf.write_bytes(b"source pdf")
        self.old_output_dir.mkdir(parents=True)
        original_xml = (
            '<quiz><question type="multichoice"><name><text>Question</text></name>'
            '<questiontext><text>Question text</text></questiontext></question></quiz>'
        )
        self.old_xml.write_text(original_xml, encoding="utf-8")
        manifest = {
            "schema_version": 1,
            "status": "completed",
            "source_pdf": "keep401.pdf",
            "source_sha256": hashlib.sha256(self.source_pdf.read_bytes()).hexdigest(),
            "configuration": {
                "communicator": "AgentSessionBackend",
                "languages": ["english"],
                "standards": "neet_ug",
                "tags": "",
                "batch_size": 5,
                "rules_sha256": "rules",
            },
            "output_file": self.old_xml.name,
            "batch_ids": ["pages_1_5"],
            "batches": [],
            "final_sha256": hashlib.sha256(self.old_xml.read_bytes()).hexdigest(),
        }
        self.old_manifest.write_text(json.dumps(manifest), encoding="utf-8")
        self.old_batch_dir.mkdir()
        (self.old_batch_dir / "pages_1_5.response.txt").write_text("saved response", encoding="utf-8")
        (self.old_batch_dir / "pages_1_5.xml").write_text("<quiz />", encoding="utf-8")
        (self.old_work_dir / "markdown").mkdir(parents=True)
        (self.old_work_dir / "markdown" / "keep401.md").write_text("saved OCR", encoding="utf-8")

    def test_apply_copies_checkpoint_and_rebases_category_for_resume(self):
        plan = migrate_outputs(
            self.source_root,
            self.output_root,
            self.pipeline_root,
            apply=True,
            rebuild_combined=False,
        )

        self.assertEqual(len(plan.items), 1)
        target_dir = (
            self.output_root
            / "ncert-exemplar-question-bank"
            / "class11"
            / "biology"
        )
        target_xml = target_dir / "keep401_neet_ug_english.xml"
        target_manifest = target_dir / "keep401_neet_ug_english.extract.json"
        target_batch_dir = target_dir / ".keep401_neet_ug_english.extract_batches"

        quiz = ET.parse(target_xml).getroot()
        self.assertEqual(
            quiz[0].findtext("category/text"),
            "$module$/top/NCERT Exemplar Question Bank/"
            "ncert-exemplar-question-bank/class11/biology/keep401/english",
        )
        manifest = json.loads(target_manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest["source_pdf"], "keep401.pdf")
        self.assertEqual(manifest["output_file"], target_xml.name)
        self.assertEqual(
            manifest["configuration"]["category_path"],
            quiz[0].findtext("category/text"),
        )
        self.assertEqual(
            manifest["final_sha256"],
            hashlib.sha256(target_xml.read_bytes()).hexdigest(),
        )
        self.assertTrue((target_batch_dir / "pages_1_5.response.txt").is_file())
        self.assertTrue((target_dir / "keep401" / "markdown" / "keep401.md").is_file())
        self.assertTrue(self.old_xml.is_file())
        self.assertTrue(self.old_manifest.is_file())

    def test_dry_run_skips_xml_changed_after_checkpoint_without_writing(self):
        manifest = json.loads(self.old_manifest.read_text(encoding="utf-8"))
        manifest["final_sha256"] = "0" * 64
        self.old_manifest.write_text(json.dumps(manifest), encoding="utf-8")

        plan = migrate_outputs(
            self.source_root,
            self.output_root,
            self.pipeline_root,
            apply=False,
        )

        self.assertEqual(plan.items, [])
        self.assertEqual(len(plan.skipped), 1)
        self.assertIn("output changed since its checkpoint", plan.skipped[0])
        self.assertFalse(
            (self.output_root / "ncert-exemplar-question-bank").exists()
        )
        self.assertTrue(self.old_xml.is_file())

    def test_explicitly_accepts_changed_xml_and_updates_migrated_checkpoint_hash(self):
        edited_xml = self.old_xml.read_text(encoding="utf-8").replace(
            "Question text",
            "Accepted edited text",
        )
        self.old_xml.write_text(edited_xml, encoding="utf-8")
        selected_manifest = (
            "ncert_exemplar/class11/biology/keep401_neet_ug_english.extract.json"
        )
        target_root = self.output_root / "ncert-exemplar-question-bank"
        target_dir = target_root / "class11" / "biology"
        target_work_dir = target_dir / "keep401"
        target_work_dir.mkdir(parents=True)
        (target_work_dir / "preserved.md").write_text("already migrated", encoding="utf-8")
        (target_root / "unrelated.txt").write_text("preserve", encoding="utf-8")

        plan = migrate_outputs(
            self.source_root,
            self.output_root,
            self.pipeline_root,
            apply=True,
            rebuild_combined=False,
            selected_manifests={selected_manifest},
            accepted_changed_manifests={selected_manifest},
            merge_existing=True,
        )

        target_xml = target_dir / "keep401_neet_ug_english.xml"
        target_manifest = target_xml.with_suffix(".extract.json")
        migrated_manifest = json.loads(target_manifest.read_text(encoding="utf-8"))
        self.assertEqual(len(plan.items), 1)
        self.assertIn("Accepted edited text", target_xml.read_text(encoding="utf-8"))
        self.assertEqual(
            migrated_manifest["final_sha256"],
            hashlib.sha256(target_xml.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            migrated_manifest["configuration"]["category_path"],
            ET.parse(target_xml).getroot()[0].findtext("category/text"),
        )
        self.assertTrue((target_dir / ".keep401_neet_ug_english.extract_batches").is_dir())
        self.assertEqual(
            (target_work_dir / "preserved.md").read_text(encoding="utf-8"),
            "already migrated",
        )
        self.assertEqual((target_root / "unrelated.txt").read_text(encoding="utf-8"), "preserve")
        self.assertEqual(self.old_xml.read_text(encoding="utf-8"), edited_xml)
        self.assertNotEqual(
            json.loads(self.old_manifest.read_text(encoding="utf-8"))["final_sha256"],
            hashlib.sha256(self.old_xml.read_bytes()).hexdigest(),
        )

    def test_migrates_checkpoint_free_legacy_xml_without_creating_manifests(self):
        root = Path(self.temporary_directory.name)
        source_root = root / "legacy-source"
        output_root = root / "legacy-output"
        source_pdf = (
            source_root
            / "previous-year-question-bank"
            / "neet-ug"
            / "2023"
            / "07-05-2023"
            / "english-1e"
            / "paper-20231108004734.pdf"
        )
        source_pdf.parent.mkdir(parents=True)
        source_pdf.write_bytes(b"legacy source pdf")

        old_output_dir = (
            output_root
            / "neet_ug"
            / "2023"
            / "07-05-2023"
            / "English 1E"
        )
        old_output_dir.mkdir(parents=True)
        legacy_outputs = {
            "Paper_20231108004734_neet_ug_english.xml": ["english"],
            "Paper_20231108004734_neet_ug_english_bengali.xml": [
                "english",
                "bengali",
            ],
            "Paper_20231108004734_neet_ug_english_hindi.xml": [
                "english",
                "hindi",
            ],
        }
        question_xml = (
            '<quiz><question type="multichoice"><name><text>Question</text></name>'
            '<questiontext><text>Question text</text></questiontext></question></quiz>'
        )
        for filename in legacy_outputs:
            (old_output_dir / filename).write_text(question_xml, encoding="utf-8")
        placeholder = old_output_dir / "Paper_20231108004734.xml"
        placeholder.write_text(
            '<quiz><question type="category"><category><text>Old</text></category>'
            '</question></quiz>',
            encoding="utf-8",
        )

        dry_run = migrate_legacy_xml_outputs(
            source_root,
            output_root,
            self.pipeline_root,
            years={"2023"},
            apply=False,
            rebuild_combined=False,
        )
        self.assertEqual(len(dry_run.items), 3)
        self.assertEqual(len(dry_run.skipped), 1)
        self.assertIn("contains no question content", dry_run.skipped[0])
        self.assertFalse((output_root / "previous-year-question-bank").exists())

        plan = migrate_legacy_xml_outputs(
            source_root,
            output_root,
            self.pipeline_root,
            years={"2023"},
            apply=True,
            rebuild_combined=False,
        )

        target_dir = output_root.joinpath(
            source_pdf.relative_to(source_root).parent,
        )
        for languages in legacy_outputs.values():
            language_string = ",".join(languages)
            output_stem = f"{source_pdf.stem}_neet_ug_{language_string.replace(',', '_')}"
            target_xml = target_dir / f"{output_stem}.xml"
            self.assertTrue(target_xml.is_file())
            self.assertFalse(target_xml.with_suffix(".extract.json").exists())
            quiz = ET.parse(target_xml).getroot()
            self.assertEqual(
                quiz[0].findtext("category/text"),
                "$module$/top/Previous Year Question Bank/"
                "previous-year-question-bank/neet-ug/2023/07-05-2023/"
                "english-1e/paper-20231108004734/"
                f"{'_'.join(languages)}",
            )
        self.assertEqual(len(plan.items), 3)
        self.assertTrue(placeholder.is_file())
        self.assertTrue(all((old_output_dir / name).is_file() for name in legacy_outputs))

    def test_all_years_matches_legacy_2013_names_and_skips_checkpointed_xml(self):
        root = Path(self.temporary_directory.name)
        source_root = root / "legacy-source"
        output_root = root / "legacy-output"
        source_pdf = (
            source_root
            / "previous-year-question-bank"
            / "neet-ug"
            / "2013"
            / "neet-ug-2013.pdf"
        )
        source_pdf.parent.mkdir(parents=True)
        source_pdf.write_bytes(b"2013 source pdf")

        old_output_dir = output_root / "neet_ug" / "2013"
        old_output_dir.mkdir(parents=True)
        question_xml = (
            '<quiz><question type="multichoice"><name><text>Question</text></name>'
            '<questiontext><text>Question text</text></questiontext></question></quiz>'
        )
        old_outputs = [
            old_output_dir / "NEET_UG_2013_neet_ug_english.xml",
            old_output_dir / "NEET_UG_2013_neet_ug_english_bengali.xml",
            old_output_dir / "NEET_UG_2013_neet_ug_english_hindi.xml",
        ]
        for old_output in old_outputs:
            old_output.write_text(question_xml, encoding="utf-8")
        old_outputs[2].with_suffix(".extract.json").write_text("{}", encoding="utf-8")

        plan = migrate_legacy_xml_outputs(
            source_root,
            output_root,
            self.pipeline_root,
            years=None,
            apply=False,
            rebuild_combined=False,
        )

        self.assertEqual(len(plan.items), 2)
        self.assertEqual(len(plan.skipped), 1)
        self.assertIn("checkpoint exists", plan.skipped[0])
        target_dir = (
            output_root
            / "previous-year-question-bank"
            / "neet-ug"
            / "2013"
        )
        self.assertEqual(
            {item.target_output.name for item in plan.items},
            {
                "neet-ug-2013_neet_ug_english.xml",
                "neet-ug-2013_neet_ug_english_bengali.xml",
            },
        )
        self.assertFalse(target_dir.exists())


if __name__ == "__main__":
    unittest.main()

import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "combine_ncert_exemplar_xml.py"


class CombineExemplarTests(unittest.TestCase):
    def run_combiner(self, root, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(root), "--class", "class11",
             "--subject", "biology", *args],
            capture_output=True, text=True, check=False,
        )

    def test_combines_languages_and_preserves_question_content(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            folder = root / "class11" / "biology"
            folder.mkdir(parents=True)
            source = (
                '<quiz><question type="multichoice"><name><text>Question</text></name>'
                '<questiontext><text><![CDATA[<p>A &amp; B</p>]]></text>'
                '<file name="diagram.png" encoding="base64">YWJj</file>'
                '</questiontext></question></quiz>'
            )
            for suffix in ("english", "english_bengali", "english_hindi", "english_tamil"):
                (folder / f"keep401_neet_ug_{suffix}.xml").write_text(source, encoding="utf-8")
            output = root / "combined"
            result = self.run_combiner(
                root, "--languages", "english,bengali,hindi,tamil", "--output-root", str(output)
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            quiz = ET.parse(output / "class11" / "biology" / "class11_biology_combined_moodle.xml").getroot()
            questions = list(quiz)
            self.assertEqual(len(questions), 8)
            for index, language in enumerate(("English", "Bengali", "Hindi", "Tamil")):
                category, question = questions[index * 2:index * 2 + 2]
                self.assertEqual(
                    category.findtext("category/text"),
                    f"$course$/top/NCERT Exemplar/Class 11/Biology/keep401/{language}",
                )
                self.assertEqual(question.findtext("questiontext/text"), "<p>A &amp; B</p>")
                self.assertEqual(question.findtext("questiontext/file"), "YWJj")

    def test_missing_or_unexpected_language_does_not_overwrite_output(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            folder = root / "class11" / "biology"
            folder.mkdir(parents=True)
            (folder / "keep401_neet_ug_english.xml").write_text("<quiz/>", encoding="utf-8")
            output = folder / "class11_biology_combined_moodle.xml"
            output.write_text("unchanged", encoding="utf-8")
            missing = self.run_combiner(root)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("missing XML", missing.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), "unchanged")

            (folder / "keep401_neet_ug_english_tamil.xml").write_text("<quiz/>", encoding="utf-8")
            unexpected = self.run_combiner(root, "--languages", "english")
            self.assertNotEqual(unexpected.returncode, 0)
            self.assertIn("not listed in --languages", unexpected.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), "unchanged")

    def test_empty_xml_is_reported_and_category_is_retained(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            folder = root / "class11" / "biology"
            folder.mkdir(parents=True)
            (folder / "keep401_neet_ug_english.xml").write_text("<quiz/>", encoding="utf-8")
            result = self.run_combiner(root, "--languages", "english")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("has no questions", result.stderr)
            quiz = ET.parse(folder / "class11_biology_combined_moodle.xml").getroot()
            self.assertEqual(len(quiz), 1)
            self.assertEqual(quiz[0].get("type"), "category")


if __name__ == "__main__":
    unittest.main()

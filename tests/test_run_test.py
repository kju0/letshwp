"""scripts/run_test.py(테스트 모드 실행기) 자동 테스트."""

import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_test import run_test, main, RunTestError  # noqa: E402
from hwpx_analysis.package import sha256_of  # noqa: E402
from hwpx_fixture import build_fixture  # noqa: E402

EXPECTED_FILES = {"source.hwpx", "inspection.json", "inspection_report.txt",
                  "check.json", "check_report.txt", "run_info.json"}


class RunTestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "doc.hwpx"
        build_fixture(self.src)
        self.out_root = self.dir / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_all_outputs_and_keeps_source(self):
        before = sha256_of(self.src)
        out_dir, info = run_test(self.src, out_root=self.out_root)
        self.assertEqual({p.name for p in out_dir.iterdir()}, EXPECTED_FILES)
        self.assertEqual(sha256_of(self.src), before)
        self.assertTrue(info["ok"], info["errors"])
        self.assertTrue(info["source_unchanged"])
        self.assertEqual(sha256_of(out_dir / "source.hwpx"), before)

    def test_run_info_records_code_version_and_summary(self):
        out_dir, _ = run_test(self.src, out_root=self.out_root)
        info = json.loads((out_dir / "run_info.json").read_text(encoding="utf-8"))
        self.assertIn("commit", info["code"])
        self.assertIn("uncommitted_code_changes", info["code"])
        check = json.loads((out_dir / "check.json").read_text(encoding="utf-8"))
        s = check["summary"]
        self.assertEqual(info["check_summary"]["total"], s["total"])
        self.assertEqual(sum(info["check_summary"][k] for k in ("PASS", "FAIL", "UNKNOWN", "SKIP")), s["total"])
        self.assertIn("①", (out_dir / "check_report.txt").read_text(encoding="utf-8"))

    def test_doc_type_passed_to_checker(self):
        out_dir, info = run_test(self.src, doc_type="B", out_root=self.out_root)
        check = json.loads((out_dir / "check.json").read_text(encoding="utf-8"))
        self.assertEqual(info["doc_type"], "B")
        self.assertEqual(check["document"]["document_type"]["value"], "B")

    def test_separate_folder_per_run(self):
        t = datetime.datetime(2026, 1, 1, 9, 0, 0)
        a, _ = run_test(self.src, out_root=self.out_root, now=t)
        b, _ = run_test(self.src, out_root=self.out_root, now=t + datetime.timedelta(seconds=1))
        self.assertNotEqual(a, b)
        with self.assertRaises(FileExistsError):
            run_test(self.src, out_root=self.out_root, now=t)

    def test_rejects_non_hwpx(self):
        bad = self.dir / "doc.txt"
        bad.write_text("x", encoding="utf-8")
        with self.assertRaises(RunTestError):
            run_test(bad, out_root=self.out_root)
        self.assertEqual(main([str(bad), "--out-root", str(self.out_root)]), 2)


if __name__ == "__main__":
    unittest.main()

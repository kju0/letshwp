"""inspector.py 자동 테스트.

실행
    python -m unittest discover -s tests -v          # 전체 테스트
    python tests/test_inspector.py a.hwpx [b.hwpx]   # 지정한 HWPX 를 분석해 결과를 출력·저장

출력물(tests/output/)
    <이름>.inspect.json   분석 결과 JSON
    <이름>.report.txt     사람이 읽는 보고서

실제 샘플 테스트
    tests/samples/*.hwpx 또는 환경변수 HWPX_SAMPLE_DIR 의 *.hwpx 를 자동으로 분석한다.
    샘플이 없으면 해당 테스트는 건너뛴다(개인/업무 문서를 저장소에 넣지 않기 위함).
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from inspector import inspect_hwpx, save_json, InspectionError  # noqa: E402
from hwpx_analysis.package import sha256_of  # noqa: E402
from hwpx_analysis.report import render_report  # noqa: E402
from hwpx_fixture import build_fixture, BODY1  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
SAMPLE_DIR = Path(os.environ.get("HWPX_SAMPLE_DIR") or Path(__file__).resolve().parent / "samples")


def write_outputs(result, stem):
    OUTPUT_DIR.mkdir(exist_ok=True)
    json_path = save_json(result, OUTPUT_DIR / ("%s.inspect.json" % stem))
    report_path = OUTPUT_DIR / ("%s.report.txt" % stem)
    report_path.write_text(render_report(result), encoding="utf-8")
    return json_path, report_path


def para_by_text(result, prefix):
    for p in result["paragraphs"]:
        if p["text"].strip().startswith(prefix):
            return p
    raise AssertionError("문단을 찾을 수 없음: %r" % prefix)


class FixtureInspectionTest(unittest.TestCase):
    """합성 픽스처(기대값을 아는 문서)로 추출 정확성과 분류 동작을 검증한다."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="letshwp_test_")
        cls.path = Path(cls.tmp) / "fixture.hwpx"
        build_fixture(cls.path)
        cls.sha_before = sha256_of(cls.path)
        cls.mtime_before = cls.path.stat().st_mtime_ns
        cls.result = inspect_hwpx(cls.path)
        write_outputs(cls.result, "fixture")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # --- 원본 보호 -------------------------------------------------------
    def test_source_is_not_modified(self):
        self.assertEqual(sha256_of(self.path), self.sha_before)
        self.assertEqual(self.path.stat().st_mtime_ns, self.mtime_before)
        self.assertTrue(self.result["source"]["unchanged_after_analysis"])
        self.assertEqual(self.result["source"]["sha256"], self.sha_before)

    def test_save_json_refuses_to_overwrite_hwpx(self):
        with self.assertRaises(InspectionError):
            save_json(self.result, self.path)
        with self.assertRaises(InspectionError):
            save_json(self.result, Path(self.tmp) / "other.hwpx")

    def test_rejects_non_hwpx(self):
        bad = Path(self.tmp) / "x.txt"
        bad.write_text("not hwpx")
        with self.assertRaises(InspectionError):
            inspect_hwpx(bad)

    # --- 패키지/검증 -----------------------------------------------------
    def test_package_structure(self):
        pkg = self.result["package"]
        self.assertEqual(pkg["mimetype"], "application/hwp+zip")
        self.assertTrue(pkg["mimetype_first_entry"])
        self.assertEqual(pkg["header_part"], "Contents/header.xml")
        self.assertEqual(pkg["section_parts"], ["Contents/section0.xml"])

    def test_verification_passes(self):
        v = self.result["verification"]
        self.assertTrue(v["all_ok"], v)
        self.assertTrue(v["text_coverage"]["ok"])
        self.assertEqual(v["unhandled_elements"], {})

    # --- 페이지 설정 -----------------------------------------------------
    def test_page_setup(self):
        page = self.result["document"]["sections"][0]["properties"]["page"]
        self.assertEqual(page["paper"], "A4")
        self.assertEqual(page["orientation_by_dimensions"], "PORTRAIT")
        self.assertEqual(page["landscape_attr_raw"], "WIDELY")
        m = page["margin_mm"]
        self.assertEqual((m["top"], m["bottom"], m["left"], m["right"], m["header"], m["footer"]),
                         (20.0, 10.0, 20.0, 20.0, 10.0, 10.0))

    # --- 글자/문단 서식 --------------------------------------------------
    def test_char_format_extraction(self):
        cf = para_by_text(self.result, "보고서 제목")["char_format"]
        self.assertEqual((cf["font_hangul"], cf["size_pt"], cf["ratio_hangul"], cf["spacing_hangul"]),
                         ("HY헤드라인M", 20.0, 100, -2))
        body = para_by_text(self.result, BODY1[:10])["char_format"]
        self.assertEqual((body["font_hangul"], body["size_pt"], body["bold"]), ("휴먼명조", 12.0, False))
        self.assertTrue(para_by_text(self.result, "1. 현황")["char_format"]["bold"])

    def test_para_format_uses_switch_case_values(self):
        pf = para_by_text(self.result, "Ⅰ.")["para_format"]
        self.assertEqual((pf["align"], pf["line_spacing"]), ("JUSTIFY", "160%"))
        # case 분기(10pt/5pt)를 채택해야 하며 default 분기(20pt/10pt)를 쓰면 안 된다
        self.assertEqual((pf["space_before_pt"], pf["space_after_pt"]), (10.0, 5.0))
        self.assertTrue(pf["value_source"].startswith("switch:case"))
        pp = self.result["format_definitions"]["para_properties"][str(pf["para_pr_id"])]
        self.assertEqual(pp["margin_switch_default_raw"]["prev_pt"], 20.0)

    def test_heading_inside_switch(self):
        pp = self.result["format_definitions"]["para_properties"]["5"]
        self.assertEqual((pp["heading"]["type"], pp["heading"]["level"]), ("OUTLINE", 8))
        self.assertIn("2016/paragraph", pp["heading"]["source"])
        self.assertEqual(pp["heading"]["switch_default_raw"]["type"], "NONE")

    def test_note_line_spacing_and_indent(self):
        pf = para_by_text(self.result, "※")["para_format"]
        self.assertEqual((pf["line_spacing"], pf["left_margin_pt"]), ("130%", 10.0))

    def test_mixed_content_text_and_mixed_char_format(self):
        p = para_by_text(self.result, "세부 항목은")
        self.assertIn("\t", p["text"])
        self.assertIn("\n", p["text"])
        self.assertTrue(p["char_format_mixed"])
        self.assertEqual(p["char_format"]["bold"], False)  # 대표값은 글자 수가 많은 보통 글씨

    # --- 표 ------------------------------------------------------------
    def test_table_structure_and_cell_formats(self):
        self.assertEqual(len(self.result["tables"]), 1)
        t = self.result["tables"][0]
        self.assertEqual((t["rows"], t["cols"]), (3, 2))
        self.assertTrue(t["grid_check"]["ok"])
        self.assertEqual(t["grid_check"]["merged_cells"], 1)
        first = [c for c in t["cells"] if c["row"] == 0][0]
        self.assertEqual((first["col_span"], first["is_header"], first["vert_align"]), (2, True, "CENTER"))
        self.assertEqual(first["fill"]["face_color"], "#D6D6D6")
        self.assertEqual(first["margin_source"], "table.inMargin")
        f = t["text_format_summary"]["all_cells_formats"]
        self.assertEqual(f["font_hangul"][0], {"value": "맑은 고딕", "count": 5, "ratio": 1.0})
        self.assertEqual(f["align"][0]["value"], "CENTER")
        self.assertEqual(f["line_spacing"][0]["value"], "110%")
        self.assertEqual(f["ratio"][0]["value"], 95)
        self.assertEqual(f["spacing"][0]["value"], -5)

    def test_picture(self):
        self.assertEqual(len(self.result["pictures"]), 1)
        g = self.result["pictures"][0]
        self.assertEqual(g["binary_path"], "BinData/image1.png")
        self.assertEqual(g["current_size"], {"width_mm": 100.0, "height_mm": 50.0})

    # --- 역할 분류 -------------------------------------------------------
    def assertRole(self, prefix, role):
        r = para_by_text(self.result, prefix)["role"]
        self.assertEqual(r["role"], role, "%s: %s" % (prefix, json.dumps(r, ensure_ascii=False)[:600]))
        self.assertTrue(r["evidence"], "근거가 비어 있음")
        self.assertGreaterEqual(r["confidence"], 0.0)
        self.assertLessEqual(r["confidence"], 1.0)
        return r

    def test_roles(self):
        self.assertRole("보고서 제목", "document_title")
        self.assertRole("Ⅰ. 추진", "heading_1")
        self.assertRole("1. 현황", "heading_2")
        self.assertRole(BODY1[:10], "body")
        self.assertRole("※ 자료", "note")
        self.assertRole("<표 1>", "table_title")
        self.assertRole("<그림 1>", "figure_caption")
        self.assertRole("머리말 문구", "header_footer")
        self.assertRole("각주 내용", "note")
        self.assertRole("2024", "table_cell")

    def test_structural_roles(self):
        roles = [p["role"]["role"] for p in self.result["paragraphs"]]
        self.assertIn("empty", roles)
        self.assertEqual(roles.count("object_anchor"), 2)  # 표 문단, 그림 문단

    def test_ambiguous_paragraph_is_unclassified_with_reason(self):
        r = self.assertRole("기타", "unclassified")
        self.assertTrue(r["unclassified_reason"])
        self.assertLess(r["confidence"], 0.6)

    def test_every_paragraph_has_role_evidence_and_confidence(self):
        for p in self.result["paragraphs"]:
            r = p["role"]
            self.assertIn("role", r)
            self.assertIn("confidence", r)
            self.assertTrue(r["evidence"], p["index"])

    def test_caption_adjacency_evidence(self):
        r = para_by_text(self.result, "<그림 1>")["role"]
        self.assertTrue(any(e["rule"] == "c_adjacent" for e in r["evidence"]))

    # --- 집계 ------------------------------------------------------------
    def test_statistics(self):
        st = self.result["statistics"]
        self.assertEqual(st["paragraph_count"], len(self.result["paragraphs"]))
        fonts = {i["value"]: i["count"] for i in st["by_chars"]["font_hangul"]}
        self.assertEqual(set(fonts), {"휴먼명조", "HY헤드라인M", "맑은 고딕"})
        self.assertEqual(fonts["맑은 고딕"], len("연도별 실적20241,23420255,678"))
        self.assertGreater(st["format_signature_count"], 3)
        self.assertIn("table_cell", st["by_scope"])

    def test_json_roundtrip(self):
        out = OUTPUT_DIR / "fixture.inspect.json"
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], self.result["schema_version"])
        self.assertEqual(len(data["paragraphs"]), len(self.result["paragraphs"]))


def _sample_files():
    if not SAMPLE_DIR.is_dir():
        return []
    return sorted(SAMPLE_DIR.glob("*.hwpx"))


@unittest.skipUnless(_sample_files(), "샘플 HWPX 없음 (tests/samples/ 또는 HWPX_SAMPLE_DIR)")
class SampleInspectionTest(unittest.TestCase):
    """실제 HWPX 샘플: 분석 결과를 tests/output 에 저장하고 무결성 검증을 확인한다."""

    def test_samples(self):
        for f in _sample_files():
            with self.subTest(sample=f.name):
                sha = sha256_of(f)
                result = inspect_hwpx(f)
                write_outputs(result, f.stem)
                self.assertEqual(sha256_of(f), sha, "원본이 변경됨")
                v = result["verification"]
                self.assertTrue(v["all_ok"], json.dumps(v, ensure_ascii=False)[:800])
                for p in result["paragraphs"]:
                    self.assertTrue(p["role"]["evidence"])


def run_cli(paths):
    """지정 HWPX 를 분석해 보고서를 출력하고 tests/output 에 저장한다."""
    code = 0
    for p in paths:
        result = inspect_hwpx(p)
        json_path, report_path = write_outputs(result, Path(p).stem)
        print(render_report(result, max_paragraphs=200))
        print("JSON  : %s\n보고서: %s\n" % (json_path, report_path))
        if not result["verification"]["all_ok"]:
            code = 2
    return code


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a.lower().endswith(".hwpx")]
    if args:
        sys.exit(run_cli(args))
    unittest.main()

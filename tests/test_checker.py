"""checker.py 자동 테스트.

실행
    python3 -m unittest discover -s tests -v

합성 픽스처(tests/hwpx_fixture.py)를 Inspector 로 분석한 실제 결과를 사용하고,
시나리오별로 그 결과를 복사·변형해 판정 규칙을 검증한다.
"""

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from inspector import inspect_hwpx, save_json as save_inspection  # noqa: E402
from checker import (check_document, load_config, run, save_json, save_report, CheckerError)  # noqa: E402
from hwpx_check.report import render_report  # noqa: E402
from hwpx_check.rules import find_dates, find_times  # noqa: E402
from hwpx_analysis.package import sha256_of  # noqa: E402
from hwpx_fixture import build_fixture, BODY1  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
REQUIRED_FIELDS = ("id", "rule_id", "rule_name", "target", "role", "current", "expected", "status", "reason", "fix")


def find(results, rule_id, prefix=None, status=None):
    out = []
    for r in results:
        if r["rule_id"] != rule_id:
            continue
        if prefix is not None and not (r["target"].get("text_excerpt") or "").startswith(prefix):
            continue
        if status is not None and r["status"] != status:
            continue
        out.append(r)
    return out


def one(results, rule_id, prefix=None, **kw):
    rs = find(results, rule_id, prefix, **kw)
    assert len(rs) == 1, "%s/%s: %d건 %s" % (rule_id, prefix, len(rs), [r["status"] for r in rs])
    return rs[0]


def para(inspection, prefix):
    for p in inspection["paragraphs"]:
        if p["text"].strip().startswith(prefix):
            return p
    raise AssertionError(prefix)


class CheckerFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="letshwp_check_"))
        cls.hwpx = cls.tmp / "fixture.hwpx"
        build_fixture(cls.hwpx)
        cls.inspection = inspect_hwpx(cls.hwpx)
        cls.config, cls.config_path = load_config()
        cls.result = check_document(cls.inspection, cls.config, cls.config_path)
        cls.results = cls.result["results"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def check(self, inspection):
        return check_document(inspection, self.config, self.config_path)["results"]

    # 1. 표준값과 일치 -> PASS
    def test_pass_when_matches_standard(self):
        r = one(self.results, "FONT", BODY1[:10])
        self.assertEqual(r["status"], "PASS")
        self.assertEqual((r["current"]["value"], r["expected"]["value"]), ("휴먼명조", "휴먼명조"))
        self.assertEqual(one(self.results, "LINE_SPACING", BODY1[:10])["status"], "PASS")
        self.assertEqual(one(self.results, "PAPER_SIZE")["status"], "PASS")

    # 2. 표준값과 다름 -> FAIL (+ 수정 동작)
    def test_fail_when_differs(self):
        r = one(self.results, "FONT", "1. 현황")
        self.assertEqual(r["status"], "FAIL")
        self.assertEqual((r["current"]["value"], r["expected"]["value"]), ("휴먼명조", "HY헤드라인M"))
        self.assertTrue(r["fix"]["possible"])
        self.assertEqual(r["fix"]["mode"], "auto_if_role_confirmed")
        a = r["fix"]["actions"][0]
        self.assertEqual((a["op"], a["from"], a["to"], a["target_font_registered"]),
                         ("set_char_font", "휴먼명조", "HY헤드라인M", True))
        sp = one(self.results, "CHAR_SPACING", "보고서 제목")
        self.assertEqual((sp["status"], sp["current"]["value"], sp["fix"]["mode"]), ("FAIL", -2, "auto"))

    # 3. 분석 정보 없음 -> UNKNOWN (FAIL 아님)
    def test_unknown_when_data_missing(self):
        ins = copy.deepcopy(self.inspection)
        para(ins, BODY1[:10])["para_format"] = None
        ins["document"]["sections"][0]["properties"]["page"] = None
        results = self.check(ins)
        r = one(results, "LINE_SPACING", BODY1[:10])
        self.assertEqual(r["status"], "UNKNOWN")
        self.assertIn("needed_info", r)
        self.assertEqual(one(results, "PAPER_SIZE")["status"], "UNKNOWN")
        self.assertEqual(one(results, "PAPER_ORIENTATION")["status"], "UNKNOWN")

    # 4. 역할 불확실 -> UNKNOWN
    def test_unknown_when_role_uncertain(self):
        r = one(self.results, "FONT", "기타")
        self.assertEqual(r["status"], "UNKNOWN")
        self.assertEqual(r["role"]["role"], "unclassified")
        self.assertEqual(r["current"]["value"], "휴먼명조")  # 현재값은 실제 추출값으로 제공
        ins = copy.deepcopy(self.inspection)
        para(ins, BODY1[:10])["role"]["confidence"] = 0.4
        r2 = one(self.check(ins), "FONT", BODY1[:10])
        self.assertEqual(r2["status"], "UNKNOWN")
        self.assertIn("신뢰도", r2["unknown_reason"])

    # 5. 표준값 미확정 -> UNKNOWN/SKIP
    def test_undecided_standard(self):
        self.assertEqual(one(self.results, "FONT_SIZE")["status"], "UNKNOWN")
        title = one(self.results, "FONT", "보고서 제목")
        self.assertEqual(title["status"], "UNKNOWN")
        self.assertIn("표지", title["unknown_reason"])

    def test_font_other_elements_are_human_myeongjo(self):
        # 개정 명세: 소제목 및 기타 요소(표 내부·머리말 등)는 휴먼명조
        ins = copy.deepcopy(self.inspection)
        para(ins, "1. 현황")["role"].update(role="heading_3", label="소제목")
        r = one(self.check(ins), "FONT", "1. 현황")
        self.assertEqual((r["status"], r["expected"]["value"]), ("PASS", "휴먼명조"))
        cell = one(self.results, "FONT", "2024")
        self.assertEqual((cell["status"], cell["current"]["value"], cell["expected"]["value"]),
                         ("FAIL", "맑은 고딕", "휴먼명조"))
        self.assertEqual(one(self.results, "FONT", "머리말 문구")["status"], "PASS")

    def test_analysis_only_mismatch_is_not_fail(self):
        ins = copy.deepcopy(self.inspection)
        ins["document"]["sections"][0]["properties"]["page"]["margin_mm"]["header"] = 15.0
        r = one(self.check(ins), "HEADER_FOOTER_MARGIN")
        self.assertEqual(r["status"], "UNKNOWN")
        self.assertEqual(r["observation"], "표준값과 다름")
        self.assertEqual(one(self.results, "HEADER_FOOTER_MARGIN")["status"], "PASS")
        self.assertEqual(one(self.results, "PAGE_MARGIN")["status"], "UNKNOWN")

    # 6. 검사 제외
    def test_skip_items(self):
        self.assertEqual(one(self.results, "WORD_BREAK")["status"], "SKIP")
        skips = find(self.results, "LINE_SPACING", status="SKIP")
        groups = {tuple(r["role"]["roles"]) for r in skips}
        self.assertIn(("머리말·꼬리말",), groups)
        self.assertIn(("대제목",), groups)
        spacing_skips = find(self.results, "PARA_SPACING", status="SKIP")
        self.assertTrue(any("본문 최상위 문단이 아님" in r["unknown_reason"] for r in spacing_skips))
        for r in skips + spacing_skips:
            self.assertEqual(r["target"]["type"], "paragraph_group")
            self.assertTrue(r["unknown_reason"])

    # 7. 문단별로 다른 기준
    def test_role_dependent_standards(self):
        self.assertEqual(one(self.results, "LINE_SPACING", BODY1[:10])["expected"]["value"]["value"], 160)
        note = one(self.results, "LINE_SPACING", "※ 자료")
        self.assertEqual((note["status"], note["expected"]["value"]["value"]), ("PASS", 130))
        cap = one(self.results, "LINE_SPACING", "<표 1>")
        self.assertEqual((cap["status"], cap["expected"]["value"]["value"]), ("FAIL", 110))
        self.assertEqual(one(self.results, "FONT", "Ⅰ.")["expected"]["value"], "HY헤드라인M")
        self.assertEqual(one(self.results, "PARA_ALIGN", "연도별")["status"], "PASS")       # 표 셀 가운데
        self.assertEqual(one(self.results, "PARA_ALIGN", "2024")["status"], "UNKNOWN")     # 숫자 셀

    def test_text_rules_are_manual_only(self):
        for rid in ("DATE_FORMAT", "TIME_FORMAT", "ITEM_NUMBER", "ITEM_BULLET", "DOC_TITLE_BOX"):
            for r in find(self.results, rid, status="FAIL"):
                self.assertFalse(r["fix"]["possible"])
                self.assertEqual(r["fix"]["actions"], [])

    def test_fix_plan_detects_shared_definitions(self):
        plan = self.result["fix_plan"]
        font = [a for a in plan["role_based_actions"] if a["op"] == "set_char_font" and a["to"] == "HY헤드라인M"]
        self.assertEqual(len(font), 1)
        # 중제목의 charPr 를 본문 문단의 '강조' run 도 쓰므로 복제 필요
        self.assertTrue(font[0]["clone_required"])
        self.assertIn(para(self.inspection, "세부 항목은")["index"], font[0]["shared_by_other_paragraphs"])
        self.assertTrue(plan["preservation_rules"])

    def test_every_result_has_required_fields(self):
        for r in self.results:
            for f in REQUIRED_FIELDS:
                self.assertIn(f, r, r["id"])
            self.assertIn(r["status"], ("PASS", "FAIL", "UNKNOWN", "SKIP"))
            if r["status"] in ("PASS", "FAIL"):
                self.assertIsNotNone(r["current"], r["id"])   # 비교한 경우에만 PASS/FAIL
                self.assertIsNotNone(r["expected"], r["id"])
            if r["status"] in ("UNKNOWN", "SKIP"):
                self.assertTrue(r.get("unknown_reason"), r["id"])
        s = self.result["summary"]
        self.assertEqual(s["total"], len(self.results))
        self.assertEqual(s["total"], s["PASS"] + s["FAIL"] + s["UNKNOWN"] + s["SKIP"])

    def test_refuses_unverified_inspection(self):
        ins = copy.deepcopy(self.inspection)
        ins["verification"]["all_ok"] = False
        with self.assertRaises(CheckerError):
            check_document(ins, self.config)

    # 8. JSON 생성
    def test_json_output(self):
        out = save_json(self.result, self.tmp / "out" / "fixture_check.json")
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["total"], len(self.results))
        self.assertIn("fix_plan", data)
        self.assertEqual(data["document"]["file_name"], "fixture.hwpx")
        with self.assertRaises(CheckerError):
            save_json(self.result, self.hwpx)

    # 9. TXT 생성
    def test_txt_report(self):
        out = save_report(self.result, self.tmp / "out" / "fixture_check.txt")
        txt = out.read_text(encoding="utf-8")
        for head in ("① 문서 검사 요약", "② 수정이 필요한 항목", "③ 표준에 적합한 항목",
                     "④ 판단 불가 및 검사 제외 항목", "⑤ 향후 수정 계획"):
            self.assertIn(head, txt)
        self.assertIn("[FAIL] 글꼴", txt)
        self.assertIn("현재값  : 휴먼명조", txt)
        self.assertIn("표준값  : HY헤드라인M", txt)
        self.assertNotIn("쪽 번호:", txt)
        OUTPUT_DIR.mkdir(exist_ok=True)
        (OUTPUT_DIR / "fixture_check.txt").write_text(txt, encoding="utf-8")


class CheckerIntegrationTest(unittest.TestCase):
    """10~12: Inspector 연동, 한글·특수문자, 원본 불변."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="letshwp_check_int_"))
        cls.hwpx = cls.tmp / "보고서 (최종)_★ 2026.hwpx"
        build_fixture(cls.hwpx)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_hwpx_and_json_inputs_give_same_results(self):
        sha = sha256_of(self.hwpx)
        mtime = self.hwpx.stat().st_mtime_ns
        res_b, out_b, rep_b = run(str(self.hwpx), str(self.tmp / "b.json"), str(self.tmp / "b.txt"))
        # 12. 원본 불변
        self.assertEqual(sha256_of(self.hwpx), sha)
        self.assertEqual(self.hwpx.stat().st_mtime_ns, mtime)
        self.assertTrue(res_b["document"]["source_unchanged"])
        self.assertEqual(res_b["document"]["input_mode"], "hwpx")
        # 10. Inspector JSON 입력과 동일한 결과
        ins_path = save_inspection(inspect_hwpx(self.hwpx), self.tmp / "inspection.json")
        res_a, _, _ = run(str(ins_path), str(self.tmp / "a.json"))
        key = lambda res: [(r["id"], r["status"], json.dumps(r.get("current"), ensure_ascii=False)) for r in res["results"]]
        self.assertEqual(key(res_a), key(res_b))
        self.assertEqual(res_a["document"]["input_mode"], "inspector_json")

    def test_korean_and_special_characters(self):
        res, out, rep = run(str(self.hwpx), str(self.tmp / "한글 결과_※.json"), str(self.tmp / "한글 보고서_※.txt"))
        raw = out.read_text(encoding="utf-8")
        self.assertIn("보고서 (최종)_★ 2026.hwpx", raw)  # ensure_ascii=False
        self.assertIn("※ 자료", raw)
        self.assertIn("보고서 (최종)_★ 2026.hwpx", rep.read_text(encoding="utf-8"))

    def test_rejects_unknown_input(self):
        bad = self.tmp / "x.txt"
        bad.write_text("x", encoding="utf-8")
        with self.assertRaises(CheckerError):
            run(str(bad))
        notins = self.tmp / "y.json"
        notins.write_text('{"a": 1}', encoding="utf-8")
        with self.assertRaises(CheckerError):
            run(str(notins))


class TextPatternTest(unittest.TestCase):
    def test_dates(self):
        ok = find_dates("2015. 4. 2. 시행")
        self.assertEqual((ok[0]["raw"], ok[0]["canonical"]), ("2015. 4. 2.", "2015. 4. 2."))
        self.assertEqual(find_dates("'15. 4. 2.")[0]["canonical"], "'15. 4. 2.")
        bad = find_dates("2026.10.19.(월) 및 2026년 10월 08일")
        self.assertEqual([d["canonical"] for d in bad], ["2026. 10. 19.", "2026. 10. 8."])
        self.assertEqual(find_dates("전화 02)531-1323, 사업자 220-82-01439, 버전 1.8.0"), [])

    def test_times(self):
        kinds = {t["raw"]: t["kind"] for t in find_times("11:00까지, 오후 3시, 14시 30분 시작, 3시간 소요")}
        self.assertEqual(kinds, {"11:00": "colon", "오후 3시": "ampm", "14시 30분": "korean"})
        self.assertEqual(find_times("IP 10.1.1.1:2415"), [])


if __name__ == "__main__":
    unittest.main()

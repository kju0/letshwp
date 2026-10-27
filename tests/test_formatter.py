"""Formatter(서식 수정) 자동 테스트: XML 패치 도구, op 별 동작, 전체 수정·검증 흐름, 수정 모드 실행기."""

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import formatter  # noqa: E402
from formatter import format_hwpx  # noqa: E402
from inspector import inspect_hwpx  # noqa: E402
from hwpx_analysis.package import sha256_of  # noqa: E402
from hwpx_format.xmlpatch import scan, set_attr_edit, apply_edits, XmlPatchError  # noqa: E402
from hwpx_format import ops  # noqa: E402
from hwpx_fixture import build_fixture, SECTION  # noqa: E402
from run_fix import run_fix, main as run_fix_main  # noqa: E402

PARA_PR = (b'<hh:paraPr id="3"><hh:align horizontal="CENTER" vertical="BASELINE"/><hp:switch>'
           b'<hp:case hp:required-namespace="x"><hh:margin><hc:intent value="0" unit="HWPUNIT"/>'
           b'<hc:prev value="100" unit="HWPUNIT"/></hh:margin><hh:lineSpacing type="PERCENT" value="130" unit="HWPUNIT"/></hp:case>'
           b'<hp:default><hh:margin><hc:intent value="0" unit="HWPUNIT"/><hc:prev value="200" unit="HWPUNIT"/></hh:margin>'
           b'<hh:lineSpacing type="PERCENT" value="130" unit="HWPUNIT"/></hp:default></hp:switch></hh:paraPr>')


def _apply(op_fn, data, action):
    root, _ = scan(data)
    return apply_edits(data, op_fn(data, root, action))


class XmlPatchTest(unittest.TestCase):
    def test_set_attr_keeps_other_bytes(self):
        data = '<a x="1"  y=\'2\'><b z="가"/>텍스트</a>'.encode("utf-8")
        root, _ = scan(data)
        b = root.kids("b")[0]
        out = apply_edits(data, [set_attr_edit(data, root, "y", 5), set_attr_edit(data, b, "z", "나")])
        self.assertEqual(out.decode("utf-8"), '<a x="1"  y=\'5\'><b z="나"/>텍스트</a>')

    def test_positions_and_self_closing(self):
        data = b'<?xml version="1.0"?><r><p a="1"/><p a="2">t</p></r>'
        root, order = scan(data)
        ps = [e for e in order if e.local == "p"]
        self.assertEqual(data[ps[0].start:ps[0].end], b'<p a="1"/>')
        self.assertEqual(data[ps[1].start:ps[1].end], b'<p a="2">t</p>')

    def test_missing_attr_is_error_not_added(self):
        data = b'<a x="1"/>'
        root, _ = scan(data)
        with self.assertRaises(XmlPatchError):
            set_attr_edit(data, root, "y", 1)

    def test_overlapping_edits_rejected(self):
        with self.assertRaises(XmlPatchError):
            apply_edits(b"abcdef", [(0, 3, b"x"), (2, 4, b"y")])


class OpsTest(unittest.TestCase):
    def test_space_before_case_and_default_double(self):
        out = _apply(ops.op_set_para_space_before, PARA_PR, {"to": 6})
        self.assertIn(b'<hc:prev value="600" unit="HWPUNIT"/></hh:margin><hh:lineSpacing', out)
        self.assertIn(b'<hc:prev value="1200" unit="HWPUNIT"/>', out)
        # 반복 적용해도 누적되지 않음(목표값 설정)
        self.assertEqual(_apply(ops.op_set_para_space_before, out, {"to": 6}), out)

    def test_line_spacing_updates_both_branches(self):
        out = _apply(ops.op_set_para_line_spacing, PARA_PR, {"to": {"type": "PERCENT", "value": 160}})
        self.assertEqual(out.count(b'<hh:lineSpacing type="PERCENT" value="160" unit="HWPUNIT"/>'), 2)
        self.assertNotIn(b'value="130"', out)

    def test_hanging_indent_case_and_default_double(self):
        out = _apply(ops.op_set_para_hanging_indent, PARA_PR, {"to": -21.0})
        self.assertIn(b'<hp:case hp:required-namespace="x"><hh:margin><hc:intent value="-2100" unit="HWPUNIT"/>', out)
        self.assertIn(b'<hp:default><hh:margin><hc:intent value="-4200" unit="HWPUNIT"/>', out)
        self.assertEqual(_apply(ops.op_set_para_hanging_indent, out, {"to": -21.0}), out)

    def test_align(self):
        out = _apply(ops.op_set_para_align, PARA_PR, {"to": "JUSTIFY"})
        self.assertIn(b'<hh:align horizontal="JUSTIFY" vertical="BASELINE"/>', out)

    def test_char_spacing_only_listed_langs(self):
        data = b'<hh:charPr id="1"><hh:spacing hangul="-5" latin="-5" hanja="-5"/></hh:charPr>'
        out = _apply(ops.op_set_char_spacing, data, {"langs": ["hangul", "latin"], "to": 0})
        self.assertEqual(out, b'<hh:charPr id="1"><hh:spacing hangul="0" latin="0" hanja="-5"/></hh:charPr>')

    def test_font_not_registered_is_not_applied(self):
        data = b'<hh:charPr id="1"><hh:fontRef hangul="2"/></hh:charPr>'
        root, _ = scan(data)
        with self.assertRaises(ops.OpNotApplicable):
            ops.op_set_char_font(data, root, {"lang": "hangul", "to": "휴먼명조", "target_font_registered": False,
                                              "target_font_id": None})

    def test_page_size_keeps_orientation(self):
        a = {"to_mm": {"width": 210.0, "height": 297.0}}
        portrait = _apply(ops.op_set_page_size, b'<hp:pagePr width="61200" height="79200"/>', a)
        self.assertEqual(portrait, b'<hp:pagePr width="59528" height="84188"/>')
        landscape = _apply(ops.op_set_page_size, b'<hp:pagePr width="79200" height="61200"/>', a)
        self.assertEqual(landscape, b'<hp:pagePr width="84188" height="59528"/>')


class FormatHwpxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "doc.hwpx"
        build_fixture(self.src)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, src=None, name="out"):
        return format_hwpx(src or self.src, self.dir / name)

    def test_fixes_all_fixable_and_keeps_source(self):
        before = sha256_of(self.src)
        res = self._run()
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(sha256_of(self.src), before)
        self.assertGreater(res["summary_before"]["FAIL"], 0)
        self.assertEqual(res["summary_after"]["FAIL"], 0)
        self.assertEqual(res["fix"]["not_applied"], [])
        self.assertTrue(all(c["status"] == "PASS" for c in res["verification"]["checks"]))
        out = self.dir / "out"
        self.assertEqual({p.name for p in out.iterdir()},
                         {"doc_수정본.hwpx", "change_report.md", "fix_result.json", "check_before.json",
                          "check_before_report.txt", "check_after.json", "check_after_report.txt"})

    def test_text_structure_and_untouched_parts_preserved(self):
        res = self._run()
        fixed = Path(res["output"])
        a, b = inspect_hwpx(str(self.src)), inspect_hwpx(str(fixed))
        self.assertEqual([p["text"] for p in a["paragraphs"]], [p["text"] for p in b["paragraphs"]])
        self.assertEqual([p["path"] for p in a["paragraphs"]], [p["path"] for p in b["paragraphs"]])
        with zipfile.ZipFile(self.src) as z1, zipfile.ZipFile(fixed) as z2:
            self.assertEqual([i.filename for i in z1.infolist()], [i.filename for i in z2.infolist()])
            self.assertEqual(z2.infolist()[0].filename, "mimetype")
            self.assertEqual(z2.infolist()[0].compress_type, zipfile.ZIP_STORED)
            for n in z1.namelist():
                if n not in ("Contents/header.xml", "Contents/section0.xml"):
                    self.assertEqual(z1.read(n), z2.read(n), n)
            sec1, sec2 = z1.read("Contents/section0.xml"), z2.read("Contents/section0.xml")
        # 본문 XML 은 참조 id 속성만 바뀌고 길이 차이는 id 자릿수 차이뿐이어야 한다
        import re
        norm = lambda s: re.sub(rb'(charPrIDRef|paraPrIDRef)="\d+"', b"\\1=\"\"", s)  # noqa: E731
        self.assertEqual(norm(sec1), norm(sec2))

    def test_shared_definitions_are_cloned_not_modified(self):
        res = self._run()
        a, b = inspect_hwpx(str(self.src)), inspect_hwpx(str(res["output"]))
        defs_a, defs_b = a["format_definitions"], b["format_definitions"]
        # 기존 정의는 그대로 남고 복제본만 추가된다
        for key in ("char_properties", "para_properties"):
            old, new = defs_a[key], defs_b[key]  # id(문자열) -> 정의
            self.assertGreaterEqual(len(new), len(old))
            for i, d in old.items():
                self.assertEqual(new[i], d, "%s id=%s 가 바뀜" % (key, i))
        targets = {pi for c in res["fix"]["clones"] for pi in c["paragraphs"]}
        for pa, pb in zip(a["paragraphs"], b["paragraphs"]):
            if pa["index"] not in targets:
                self.assertEqual(pa["para_format"], pb["para_format"])
                self.assertEqual(pa["runs"], pb["runs"])

    def test_hanging_indent_fixed(self):
        res = self._run()
        self.assertIn("set_para_hanging_indent", [a["op"] for a in res["fix"]["applied"]])
        after = json.loads((self.dir / "out" / "check_after.json").read_text(encoding="utf-8"))
        aligns = [r["status"] for r in after["results"]
                  if r["rule_id"] == "HANGING_INDENT" and r.get("sub_item") == "둘째 줄 정렬"]
        self.assertTrue(aligns)
        self.assertEqual(set(aligns), {"PASS"})

    def test_idempotent_second_run(self):
        res = self._run()
        res2 = self._run(src=Path(res["output"]), name="out2")
        self.assertTrue(res2["ok"], res2["errors"])
        self.assertEqual(res2["fix"]["clones"], [])
        self.assertEqual(res2["fix"]["changed_parts"], [])
        self.assertEqual(res2["summary_before"], res2["summary_after"])

    def test_page_size_fixed(self):
        bad = self.dir / "letter.hwpx"
        with zipfile.ZipFile(self.src) as z, zipfile.ZipFile(bad, "w") as o:
            for i in z.infolist():
                data = z.read(i.filename)
                if i.filename == "Contents/section0.xml":
                    data = SECTION.replace('width="59528" height="84188"', 'width="61200" height="79200"').encode("utf-8")
                o.writestr(i, data)
        res = self._run(src=bad, name="letter_out")
        self.assertTrue(res["ok"], res["errors"])
        self.assertIn("set_page_size", [a["op"] for a in res["fix"]["applied"]])
        after = json.loads((self.dir / "letter_out" / "check_after.json").read_text(encoding="utf-8"))
        self.assertEqual([r["status"] for r in after["results"] if r["rule_id"] == "PAPER_SIZE"], ["PASS"])

    def test_verification_failure_discards_output(self):
        fail = {"all_ok": False, "checks": [{"name": "강제 실패", "status": "FAIL", "detail": "테스트"}]}
        with mock.patch.object(formatter, "verify", return_value=fail):
            res = self._run()
        self.assertFalse(res["ok"])
        self.assertIsNone(res["output"])
        self.assertFalse((self.dir / "out" / "doc_수정본.hwpx").exists())
        md = (self.dir / "out" / "change_report.md").read_text(encoding="utf-8")
        self.assertIn("검증 실패", md)

    def test_markdown_report(self):
        self._run()
        md = (self.dir / "out" / "change_report.md").read_text(encoding="utf-8")
        for s in ("# 서식 수정 보고서", "✅", "## 1. 한눈에 보기", "## 2. 수정한 내용", "### 자간",
                  "## 3. 검증 결과", "## 4. 수정하지 않은 항목", "| 위치 | 내용 | 변경 전 | 변경 후 |"):
            self.assertIn(s, md)


class RunFixTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "보고서.hwpx"
        build_fixture(self.src)

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_fix_outputs(self):
        before = sha256_of(self.src)
        out_dir, info = run_fix(self.src, out_root=self.dir / "o")
        self.assertTrue(info["ok"], info["errors"])
        self.assertTrue(out_dir.name.endswith("_수정"))
        names = {p.name for p in out_dir.iterdir()}
        self.assertIn("보고서_수정본.hwpx", names)
        self.assertIn("source.hwpx", names)
        self.assertIn("run_info.json", names)
        self.assertEqual(sha256_of(self.src), before)
        self.assertEqual(sha256_of(out_dir / "source.hwpx"), before)
        self.assertEqual(info["check_after"]["FAIL"], 0)

    def test_main_exit_codes(self):
        with mock.patch("sys.stdout"):
            self.assertEqual(run_fix_main([str(self.src), "--out-root", str(self.dir / "o2")]), 0)
            self.assertEqual(run_fix_main([str(self.dir / "없음.hwpx"), "--out-root", str(self.dir / "o3")]), 2)


if __name__ == "__main__":
    unittest.main()

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
from hwpx_fixture import build_fixture, build_pages_fixture, SECTION  # noqa: E402
from hwpx_format.verify import Remap  # noqa: E402
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

    def test_margin_value_scale_by_branch(self):
        # case(HwpUnitChar) 분기 pt×100, default 분기·switch 없는(direct) 문단 모양 pt×200
        out = _apply(ops.op_set_para_hanging_indent, PARA_PR, {"to": -35})
        self.assertIn(b'<hc:intent value="-3500" unit="HWPUNIT"/><hc:prev value="100"', out)      # case
        self.assertIn(b'<hc:intent value="-7000" unit="HWPUNIT"/><hc:prev value="200"', out)      # default
        direct = (b'<hh:paraPr id="7"><hh:margin><hc:intent value="0" unit="HWPUNIT"/><hc:prev value="0" unit="HWPUNIT"/></hh:margin>'
                  b'<hh:lineSpacing type="PERCENT" value="160" unit="HWPUNIT"/></hh:paraPr>')
        out = _apply(ops.op_set_para_hanging_indent, direct, {"to": -35})
        out = _apply(ops.op_set_para_space_before, out, {"to": 14})
        self.assertIn(b'<hc:intent value="-7000" unit="HWPUNIT"/><hc:prev value="2800" unit="HWPUNIT"/>', out)

    def test_page_margin(self):
        data = b'<hp:pagePr width="59528" height="84188"><hp:margin header="4000" footer="2834" left="5669" right="5669" top="4000" bottom="4251"/></hp:pagePr>'
        out = _apply(ops.op_set_page_margin, data, {"to_mm": {"top": 20.0, "bottom": 10.0, "header": 10.0}})
        self.assertEqual(out, b'<hp:pagePr width="59528" height="84188"><hp:margin header="2834" footer="2834" left="5669" right="5669" top="5669" bottom="2834"/></hp:pagePr>')
        self.assertEqual((ops.mm_to_hwpunit(15), ops.mm_to_hwpunit(20)), (4251, 5669))   # 한글 저장값과 일치

    def test_word_break(self):
        data = (b'<hh:paraPr id="7"><hh:breakSetting breakLatinWord="HYPHENATION" breakNonLatinWord="KEEP_WORD" '
                b'widowOrphan="0" lineWrap="BREAK"/></hh:paraPr>')
        out = _apply(ops.op_set_para_word_break, data, {"to": {"breakNonLatinWord": "BREAK_WORD", "breakLatinWord": "KEEP_WORD"}})
        self.assertIn(b'breakLatinWord="KEEP_WORD" breakNonLatinWord="BREAK_WORD" widowOrphan="0"', out)
        with self.assertRaises(ops.OpNotApplicable):
            _apply(ops.op_set_para_word_break, b'<hh:paraPr id="7"/>', {"to": {"breakNonLatinWord": "BREAK_WORD"}})

    def test_char_size(self):
        data = b'<hh:charPr id="3" height="1200"><hh:fontRef hangul="0"/></hh:charPr>'
        self.assertEqual(_apply(ops.op_set_char_size, data, {"to": 15}), b'<hh:charPr id="3" height="1500"><hh:fontRef hangul="0"/></hh:charPr>')

    def test_cell_border_sides(self):
        data = (b'<hh:borderFill id="2"><hh:leftBorder type="SOLID" width="0.12 mm"/><hh:rightBorder type="SOLID" width="0.12 mm"/>'
                b'<hh:topBorder type="SOLID" width="0.12 mm"/></hh:borderFill>')
        out = _apply(ops.op_set_cell_border, data, {"sides": ["left", "right"], "to": "NONE"})
        self.assertEqual(out, b'<hh:borderFill id="2"><hh:leftBorder type="NONE" width="0.12 mm"/><hh:rightBorder type="NONE" width="0.12 mm"/>'
                              b'<hh:topBorder type="SOLID" width="0.12 mm"/></hh:borderFill>')
        with self.assertRaises(ops.OpNotApplicable):
            _apply(ops.op_set_cell_border, data, {"sides": ["bottom"], "to": "NONE"})


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
        after = json.loads((self.dir / "out" / "check_after.json").read_text(encoding="utf-8"))
        left = [(r["rule_id"], r["target"].get("text_excerpt")) for r in after["results"] if r["status"] == "FAIL"]
        self.assertEqual(left, [("ITEM_NUMBER", "Ⅰ. 추진 배경")])      # 표준 밖 번호는 검사만(수동)
        self.assertFalse(any(r["fix"]["possible"] for r in after["results"] if r["status"] == "FAIL"))
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
        # 본문 XML 은 참조 id 속성과 추가된 쪽 번호 컨트롤 외에는 같아야 한다
        import re
        norm = lambda s: re.sub(rb'(charPrIDRef|paraPrIDRef|borderFillIDRef)="\d+"', b"\\1=\"\"", s)  # noqa: E731
        sec2 = re.sub(rb'<hp:ctrl><hp:pageNum [^>]*/></hp:ctrl>', b"", sec2, count=1)
        self.assertEqual(norm(sec1), norm(sec2))
        self.assertIn(b'<hp:ctrl><hp:pageNum pos="BOTTOM_CENTER" formatType="DIGIT" sideChar="-"/></hp:ctrl>',
                      zipfile.ZipFile(fixed).read("Contents/section0.xml"))

    def test_shared_definitions_are_cloned_not_modified(self):
        res = self._run()
        a, b = inspect_hwpx(str(self.src)), inspect_hwpx(str(res["output"]))
        defs_a, defs_b = a["format_definitions"], b["format_definitions"]
        # 기존 정의는 그대로 남고 복제본만 추가된다
        for key in ("char_properties", "para_properties", "border_fills"):
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

    def test_direct_margin_document_roundtrip(self):
        # switch 없이 저장된 문서(한글 10 build 5760 형식): 읽을 때 1pt=200 으로 환산하고, 고친 뒤 다시 읽어도 같은 값
        import re
        from hwpx_fixture import HEADER
        direct_header = re.sub(r"<hp:switch><hp:case[^>]*>(.*?)</hp:case><hp:default>.*?</hp:default></hp:switch>", r"\1", HEADER)
        direct_header = re.sub(r'<hc:(intent|left|right|prev|next) value="(-?\d+)"',
                               lambda m: '<hc:%s value="%d"' % (m.group(1), int(m.group(2)) * 2), direct_header)
        src = self.dir / "direct.hwpx"
        with zipfile.ZipFile(self.src) as z, zipfile.ZipFile(src, "w") as o:
            for i in z.infolist():
                o.writestr(i, direct_header.encode("utf-8") if i.filename == "Contents/header.xml" else z.read(i.filename))
        a = inspect_hwpx(str(src))
        self.assertEqual(a["format_definitions"]["para_properties"]["0"]["margin_source"], "direct")
        self.assertEqual(a["format_definitions"]["para_properties"]["3"]["margin"]["left_pt"], 10.0)   # 2000/200
        # 글꼴 파일 유무와 관계없이 같은 값이 나오도록 글꼴 폴더를 비운 설정으로 실행(글자 수 근사)
        import checker
        cfg, _ = checker.load_config()
        cfg["rules"]["HANGING_INDENT"]["font_dir"] = str(self.dir / "no-fonts")
        cfg_path = self.dir / "cfg.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        res = format_hwpx(src, self.dir / "direct_out", config_path=str(cfg_path))
        self.assertTrue(res["ok"], res["errors"])
        b = inspect_hwpx(str(res["output"]))
        heading = next(p for p in b["paragraphs"] if p["text"].startswith("1. 현황"))
        # 근사: '1. ' = 16pt × (0.5 + 0.5 + 0.5) = 24pt
        self.assertEqual(heading["para_format"]["indent_pt"], -24.0)
        with zipfile.ZipFile(res["output"]) as z:
            h = z.read("Contents/header.xml").decode("utf-8")
        self.assertIn('<hc:intent value="-4800" unit="HWPUNIT"/>', h)        # 24pt × 200 (switch 없는 문서)

    def test_page_margin_fixed_but_paper_size_not(self):
        bad = self.dir / "letter.hwpx"
        with zipfile.ZipFile(self.src) as z, zipfile.ZipFile(bad, "w") as o:
            for i in z.infolist():
                data = z.read(i.filename)
                if i.filename == "Contents/section0.xml":
                    data = SECTION.replace('width="59528" height="84188"', 'width="61200" height="79200"').replace(
                        'header="2835" footer="2835" gutter="0" left="5669" right="5669" top="5669" bottom="2835"',
                        'header="4000" footer="2835" gutter="0" left="5669" right="4000" top="7000" bottom="2835"').encode("utf-8")
                o.writestr(i, data)
        res = self._run(src=bad, name="letter_out")
        self.assertTrue(res["ok"], res["errors"])
        ops_applied = [a["op"] for a in res["fix"]["applied"]]
        self.assertIn("set_page_margin", ops_applied)
        self.assertNotIn("set_page_size", ops_applied)          # 용지 크기는 판정·수정 대상 아님
        after = json.loads((self.dir / "letter_out" / "check_after.json").read_text(encoding="utf-8"))
        st = {r["rule_id"]: r["status"] for r in after["results"] if r["rule_id"] in ("PAPER_SIZE", "PAGE_MARGIN", "HEADER_FOOTER_MARGIN")}
        self.assertEqual(st, {"PAPER_SIZE": "UNKNOWN", "PAGE_MARGIN": "PASS", "HEADER_FOOTER_MARGIN": "PASS"})
        pg = inspect_hwpx(str(res["output"]))["document"]["sections"][0]["properties"]["page"]
        self.assertEqual((pg["paper"], pg["margin_mm"]["top"], pg["margin_mm"]["right"], pg["margin_mm"]["header"]),
                         ("Letter", 20.0, 20.0, 10.0))

    def test_table_side_border_removed_for_edge_cells_only(self):
        res = self._run()
        self.assertTrue(res["ok"], res["errors"])
        self.assertIn("set_cell_border", [a["op"] for a in res["fix"]["applied"]])
        after = json.loads((self.dir / "out" / "check_after.json").read_text(encoding="utf-8"))
        self.assertEqual([r["status"] for r in after["results"] if r["rule_id"] == "TABLE_SIDE_BORDER"], ["PASS"])
        cells = {(c["row"], c["col"]): c["borders"] for c in inspect_hwpx(str(res["output"]))["tables"][0]["cells"]}
        self.assertEqual((cells[(0, 0)]["left"]["type"], cells[(0, 0)]["right"]["type"]), ("NONE", "NONE"))   # 2열 병합 셀
        self.assertEqual((cells[(1, 0)]["left"]["type"], cells[(1, 0)]["right"]["type"]), ("NONE", "SOLID"))  # 왼쪽 끝 열
        self.assertEqual((cells[(1, 1)]["left"]["type"], cells[(1, 1)]["right"]["type"]), ("SOLID", "NONE"))  # 오른쪽 끝 열
        self.assertEqual(cells[(1, 0)]["top"]["type"], "SOLID")                                              # 위아래 선은 그대로
        clones = [c for c in res["fix"]["clones"] if c["kind"] == "borderFill"]
        self.assertEqual(sum(len(c["cells"]) for c in clones), 5)

    def test_char_spacing_and_font_size_handling(self):
        res = self._run()
        self.assertNotIn("set_char_spacing", [a["op"] for a in res["fix"]["applied"]])   # 자간은 수정하지 않음
        self.assertIn("set_char_size", [a["op"] for a in res["fix"]["applied"]])
        b = inspect_hwpx(str(res["output"]))
        title = next(p for p in b["paragraphs"] if p["text"].startswith("보고서 제목"))
        self.assertEqual(title["char_format"]["spacing_hangul"], -2)                      # 원래 자간 유지
        heading = next(p for p in b["paragraphs"] if p["text"].startswith("1. 현황"))
        self.assertEqual((heading["char_format"]["size_pt"], heading["char_format"]["font_hangul"]), (16.0, "HY헤드라인M"))  # '1.' = 대제목
        body = next(p for p in b["paragraphs"] if p["text"].startswith("현재 사업은"))
        self.assertEqual(body["char_format"]["size_pt"], 14.0)                            # 일반 본문 14pt
        note = next(p for p in b["paragraphs"] if p["text"].startswith("※ 자료"))
        self.assertEqual(note["char_format"]["size_pt"], 12.0)                            # 수정 후 본문 14 - 2

    def test_word_break_off_by_default(self):
        # 2026-10-31 사용자 결정: 어절 단위 줄 나눔 규칙을 꺼서 원본 줄 나눔 기준을 그대로 둔다
        res = self._run()
        self.assertTrue(res["ok"], res["errors"])
        self.assertNotIn("set_para_word_break", [a["op"] for a in res["fix"]["applied"]])

    def test_word_break_set_to_word_unit(self):
        import checker
        cfg, _ = checker.load_config()
        cfg["rules"]["WORD_BREAK"]["enabled"] = True
        cfg_path = self.dir / "cfg_word_break.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        res = format_hwpx(self.src, self.dir / "out", config_path=str(cfg_path))
        self.assertTrue(res["ok"], res["errors"])
        self.assertIn("set_para_word_break", [a["op"] for a in res["fix"]["applied"]])
        after = json.loads((self.dir / "out" / "check_after.json").read_text(encoding="utf-8"))
        self.assertEqual({r["status"] for r in after["results"] if r["rule_id"] == "WORD_BREAK"}, {"PASS"})
        b = inspect_hwpx(str(res["output"]))
        for p in b["paragraphs"]:
            if not p["is_empty"]:
                self.assertEqual(p["para_format"]["break_hangul"], "BREAK_WORD", p["text"][:20])   # 한글 저장값 기준 '어절'
                self.assertEqual(p["para_format"]["break_latin"], "KEEP_WORD")
        with zipfile.ZipFile(res["output"]) as z:
            header = z.read("Contents/header.xml").decode("utf-8")
        self.assertIn('breakNonLatinWord="KEEP_WORD"', header)   # 원래 정의는 그대로(복제본만 바뀜)
        self.assertIn('breakNonLatinWord="BREAK_WORD"', header)

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
        for s in ("# 서식 수정 보고서", "✅", "## 1. 한눈에 보기", "## 2. 수정한 내용", "### 장평", "### 표 양단선",
                  "## 3. 검증 결과", "## 4. 수정하지 않은 항목", "| 위치 | 내용 | 변경 전 | 변경 후 |"):
            self.assertIn(s, md)


class PageBreakFormatTest(unittest.TestCase):
    """대제목 쪽 나눔 + 바로 앞 빈 문단 삭제(절대 원칙 예외)의 전체 흐름과 검증."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "pages.hwpx"
        build_pages_fixture(self.src)

    def tearDown(self):
        self.tmp.cleanup()

    def test_page_break_and_blank_deletion(self):
        before = inspect_hwpx(str(self.src))
        pages = [(p["text"][:6], (p.get("line_layout") or {}).get("page"), (p.get("line_layout") or {}).get("page_first"),
                  (p.get("line_layout") or {}).get("page_last")) for p in before["paragraphs"]]
        self.assertEqual(pages[4], ("2. 계획", 1, False, True))      # 1쪽 마지막 줄
        self.assertEqual(pages[7], ("3. 결과", 3, True, False))      # 3쪽 첫 문단
        self.assertEqual(pages[10], ("4. 마무리", 3, False, False))  # 쪽 중간
        res = format_hwpx(self.src, self.dir / "out")
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual([d["index"] for d in res["fix"]["deleted_paragraphs"]], [6])          # '2.'(쪽 마지막 줄) 앞 빈 문단은 유지
        self.assertEqual(res["fix"]["not_applied"], [])
        after = inspect_hwpx(str(res["output"]))
        texts = [p["text"] for p in after["paragraphs"]]
        self.assertEqual(len(texts), len(before["paragraphs"]) - 1)
        self.assertEqual([p["text"][:5] for p in after["paragraphs"] if p.get("page_break_before")], ["2. 계획", "3. 결과"])
        self.assertEqual([t for t in [p["text"] for p in before["paragraphs"]] if t], [t for t in texts if t])   # 글자 있는 문단 보존
        self.assertEqual(texts.count(""), 2)                                                                 # '2.'·'4.' 앞 빈 문단은 유지
        chk = json.loads((self.dir / "out" / "check_after.json").read_text(encoding="utf-8"))
        st = {r["target"].get("text_excerpt", "")[:5]: r["status"] for r in chk["results"] if r["rule_id"] == "PAGE_BREAK_HEADING"}
        self.assertEqual(st, {"1. 개요": "SKIP", "2. 계획": "PASS", "3. 결과": "PASS", "4. 마무": "PASS"})
        self.assertEqual(chk["summary"]["FAIL"], 0)
        names = [c["name"] for c in res["verification"]["checks"]]
        self.assertIn("삭제한 문단은 쪽 나눔 대제목 바로 앞의 빈 문단뿐", names)
        # 2회 변환: 더 지울 것도 넣을 것도 없다
        res2 = format_hwpx(Path(res["output"]), self.dir / "out2")
        self.assertTrue(res2["ok"], res2["errors"])
        self.assertEqual(res2["fix"]["deleted_paragraphs"], [])
        self.assertEqual([a["op"] for a in res2["fix"]["applied"] if a["op"] in ("set_page_break", "delete_empty_paragraph")], [])

    def test_delete_refuses_non_empty_paragraph(self):
        from hwpx_format.apply import op_delete_empty_paragraph
        from hwpx_format.ops import OpNotApplicable
        for xml in ('<hp:p id="0"><hp:run charPrIDRef="2"><hp:t>글자</hp:t></hp:run></hp:p>'.encode("utf-8"),
                    b'<hp:p id="0"><hp:run charPrIDRef="2"><hp:tbl rowCnt="1"/></hp:run></hp:p>',
                    b'<hp:p id="0"><hp:run charPrIDRef="2"><hp:ctrl><hp:colPr/></hp:ctrl></hp:run></hp:p>'):
            root, _ = scan(xml)
            with self.assertRaises(OpNotApplicable):
                op_delete_empty_paragraph(xml, root, {})
        xml = b'<r><hp:p id="0"><hp:run charPrIDRef="2"></hp:run></hp:p><hp:p id="0"><hp:run charPrIDRef="2"><hp:t></hp:t></hp:run></hp:p></r>'
        root, _ = scan(xml)
        edits = [e for p_el in root.kids("p") for e in op_delete_empty_paragraph(xml, p_el, {})]
        self.assertEqual(apply_edits(xml, edits), b"<r></r>")

    def test_remap_paths_and_indices(self):
        paras = [{"index": i} for i in range(6)]
        rm = Remap(paras, [{"index": 2, "path": "sec0/p2", "section": 0}, {"index": 4, "path": "sec0/p4", "section": 0}])
        self.assertEqual(rm.index, {0: 0, 1: 1, 3: 2, 5: 3})
        self.assertEqual(rm.path("sec0/p5"), "sec0/p3")
        self.assertEqual(rm.path("sec0/p3/tbl0/r0c0/p1"), "sec0/p2/tbl0/r0c0/p1")
        self.assertEqual(rm.path("sec0/p1"), "sec0/p1")
        self.assertEqual(rm.path("sec1/p5"), "sec1/p5")


class MarkerAndPageNumberTest(unittest.TestCase):
    """번호·기호 문자 교정(replace_marker)과 쪽 번호 추가(add_page_number)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "doc.hwpx"
        build_fixture(self.src)

    def tearDown(self):
        self.tmp.cleanup()

    def test_replace_marker_single_and_split_runs(self):
        from hwpx_format.apply import op_replace_marker
        from hwpx_format.ops import OpNotApplicable
        one = '<hp:p><hp:run charPrIDRef="2"><hp:t>  다. 필요성</hp:t></hp:run></hp:p>'.encode("utf-8")
        root, _ = scan(one)
        self.assertEqual(apply_edits(one, op_replace_marker(one, root, {"from": "다.", "to": "나."})).decode("utf-8"),
                         '<hp:p><hp:run charPrIDRef="2"><hp:t>  다. 필요성</hp:t></hp:run></hp:p>'.replace("다.", "나."))
        split = ('<hp:p><hp:run charPrIDRef="22"><hp:t> 다</hp:t></hp:run><hp:run charPrIDRef="21"><hp:t>. 디지털</hp:t></hp:run></hp:p>').encode("utf-8")
        root, _ = scan(split)
        self.assertEqual(apply_edits(split, op_replace_marker(split, root, {"from": "다.", "to": "나."})).decode("utf-8"),
                         '<hp:p><hp:run charPrIDRef="22"><hp:t> 나</hp:t></hp:run><hp:run charPrIDRef="21"><hp:t>. 디지털</hp:t></hp:run></hp:p>')
        lead = ('<hp:p><hp:run charPrIDRef="1"><hp:t>    </hp:t></hp:run><hp:run charPrIDRef="2"><hp:t>- 세부</hp:t></hp:run></hp:p>').encode("utf-8")
        root, _ = scan(lead)
        self.assertIn("<hp:t>ㅇ 세부</hp:t>", apply_edits(lead, op_replace_marker(lead, root, {"from": "-", "to": "ㅇ"})).decode("utf-8"))
        root, _ = scan(split)
        with self.assertRaises(OpNotApplicable):
            op_replace_marker(split, root, {"from": "다.", "to": "10."})        # 나뉘어 있고 길이가 다름
        root, _ = scan(one)
        with self.assertRaises(OpNotApplicable):
            op_replace_marker(one, root, {"from": "가.", "to": "나."})         # 첫 글자가 다름

    def test_page_number_added_and_number_fixed_end_to_end(self):
        import re
        bad = self.dir / "num.hwpx"
        with zipfile.ZipFile(self.src) as z, zipfile.ZipFile(bad, "w") as o:
            for i in z.infolist():
                data = z.read(i.filename)
                if i.filename == "Contents/section0.xml":
                    data = SECTION.replace("1. 현황 분석", "3. 현황 분석").encode("utf-8")
                o.writestr(i, data)
        res = format_hwpx(bad, self.dir / "out")
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(res["fix"]["marker_edits"], [{"index": 3, "from": "3.", "to": "1."}])
        self.assertEqual(res["fix"]["added_controls"], [{"index": 0, "section": 0, "control": "pageNum"}])
        after = inspect_hwpx(str(res["output"]))
        self.assertEqual(after["paragraphs"][3]["text"], "1. 현황 분석")
        self.assertEqual(after["document"]["sections"][0]["page_numbers"],
                         [{"paragraph": 0, "pos": "BOTTOM_CENTER", "format": "DIGIT", "side_char": "-"}])
        self.assertEqual(after["paragraphs"][0]["controls"], ["secPr", "colPr", "header", "pageNum"])   # 구역 설정 run 의 끝에 추가
        names = {c["name"]: c["status"] for c in res["verification"]["checks"]}
        self.assertEqual(names["글자 변경은 항목 번호·기호 문자뿐, 추가 요소는 쪽 번호뿐"], "PASS")
        chk = json.loads((self.dir / "out" / "check_after.json").read_text(encoding="utf-8"))
        st = {r["rule_id"]: r["status"] for r in chk["results"] if r["rule_id"] in ("PAGE_NUMBER",)}
        self.assertEqual(st, {"PAGE_NUMBER": "PASS"})
        self.assertEqual([r["status"] for r in chk["results"] if r["rule_id"] == "ITEM_NUMBER" and r["target"].get("text_excerpt") == "1. 현황 분석"], ["PASS"])
        # 2회 변환: 쪽 번호·번호 교정 동작 없음
        res2 = format_hwpx(Path(res["output"]), self.dir / "out2")
        self.assertTrue(res2["ok"], res2["errors"])
        self.assertEqual((res2["fix"]["marker_edits"], res2["fix"]["added_controls"]), ([], []))

    def test_existing_other_page_number_is_kept(self):
        other = self.dir / "pn.hwpx"
        with zipfile.ZipFile(self.src) as z, zipfile.ZipFile(other, "w") as o:
            for i in z.infolist():
                data = z.read(i.filename)
                if i.filename == "Contents/section0.xml":
                    data = SECTION.replace('</hp:ctrl></hp:run>', '</hp:ctrl><hp:ctrl><hp:pageNum pos="TOP_RIGHT" formatType="DIGIT" sideChar=""/></hp:ctrl></hp:run>', 1).encode("utf-8")
                o.writestr(i, data)
        res = format_hwpx(other, self.dir / "out_pn")
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(res["fix"]["added_controls"], [])
        chk = json.loads((self.dir / "out_pn" / "check_after.json").read_text(encoding="utf-8"))
        self.assertEqual([r["status"] for r in chk["results"] if r["rule_id"] == "PAGE_NUMBER"], ["UNKNOWN"])


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
        self.assertEqual(info["check_after"]["FAIL"], 1)   # 'Ⅰ.' 표준 밖 번호(수동 검토)만 남는다

    def test_main_exit_codes(self):
        with mock.patch("sys.stdout"):
            self.assertEqual(run_fix_main([str(self.src), "--out-root", str(self.dir / "o2")]), 0)
            self.assertEqual(run_fix_main([str(self.dir / "없음.hwpx"), "--out-root", str(self.dir / "o3")]), 2)


if __name__ == "__main__":
    unittest.main()

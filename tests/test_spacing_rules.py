"""개정 FORMAT_SPEC(2026-10-28 추가개발 요구사항) 규칙 테스트.

- 문단 간격: 항목 번호 단계별 위 간격(1. 20 / 가. 16 / 그 외 14), 항목 기호 층위(□ 사이 14, 한 층위 아래 6),
  ㅇ→ㅇ 6, □ 는 14(대제목 바로 다음 15 허용), 참고·주석 위 6, 일반 본문 위 6, TYPE A/B 특례, 미정 이동은 판단 불가
- 내어쓰기(주석 기호 포함, 허용 오차 0.5pt, 자간은 실제값으로 계산)
- 글자 크기(대·중제목, 주석 -2pt, □ 14pt, 목차)

문단 구성을 자유롭게 바꿔야 하므로 Inspector 결과와 같은 형태의 최소 분석 결과를 직접 만든다.
"""

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from checker import check_document, load_config, CheckerError  # noqa: E402
from hwpx_analysis.classifier import detect_list_marker, ROLE_LABELS  # noqa: E402
from hwpx_analysis.hierarchy import annotate_hierarchy, item_above_spacing, bullet_above_spacing  # noqa: E402

CONFIG, CONFIG_PATH = load_config()
# 글꼴 파일 유무와 관계없이 같은 결과가 나오도록 글꼴 폴더를 비운다(글자 수 근사). 글꼴 폭은 test_fontmetrics 에서 검사
CONFIG["rules"]["HANGING_INDENT"]["font_dir"] = "/nonexistent-letshwp-fonts"
SPACING = CONFIG["rules"]["PARA_SPACING"]


FACES = {"hangul": "휴먼명조", "latin": "휴먼명조", "symbol": "휴먼명조"}


def _cid(size):
    return 100 + int(round(size * 10))


def P(text, space_before=0.0, role=None, indent=0.0, lines=1, page_break=False, objects=None,
      container="body", space_after=0.0, auto_numbering=None, para_pr_id=None, size=14.0, confidence=0.9):
    """간이 문단. role 을 생략하면 비어 있지 않은 문단은 본문으로 둔다."""
    empty = not text.strip()
    role = role or ("object_anchor" if empty and objects else "empty" if empty else "body")
    return {
        "text": text, "is_empty": empty, "visible_length": len(text.strip()),
        "context": {"container": container, "depth": 0 if container == "body" else 1,
                    "table_id": "T9" if container == "table_cell" else None,
                    "cell": [0, 0] if container == "table_cell" else None, "object_id": None},
        "role": {"role": role, "label": ROLE_LABELS[role], "confidence": confidence, "evidence": [{"rule": "test"}]},
        "list_marker": detect_list_marker(text),
        "auto_numbering": auto_numbering,
        "objects": objects or [],
        "page_break_before": page_break,
        "para_format": {"para_pr_id": para_pr_id, "space_before_pt": space_before, "space_after_pt": space_after,
                        "indent_pt": indent, "left_margin_pt": 0.0, "align": "JUSTIFY",
                        "line_spacing_type": "PERCENT", "line_spacing_value": 160, "line_spacing": "160%"},
        "char_format": {"char_pr_id": _cid(size), "font_hangul": "휴먼명조", "size_pt": size},
        "runs": [{"char_pr_id": _cid(size), "chars": len(text)}],
        "line_layout": {"line_count": lines, "lines": [{"textpos": 0, "horzpos_pt": 0.0, "horzsize_pt": 480.0}] * lines,
                        "source": "test"},
    }


def inspection(paragraphs, app="Hancom Office Hangul", tables=None):
    char_props = {
        "0": {"id": 0, "font_face": FACES, "size_pt": 14.0,
              "ratio": {"hangul": 100, "latin": 100}, "spacing": {"hangul": 0, "latin": 0}},
        # 1: 공백만 있는 run 용 10pt 자간 -10%
        "1": {"id": 1, "font_face": FACES, "size_pt": 10.0,
              "ratio": {"hangul": 100, "latin": 100}, "spacing": {"hangul": -10, "latin": -10}},
        # 2: 자간 -4% 글자 모양(자간은 수정하지 않으므로 실제값으로 폭 계산)
        "2": {"id": 2, "font_face": FACES, "size_pt": 14.0,
              "ratio": {"hangul": 100, "latin": 100, "symbol": 100}, "spacing": {"hangul": -4, "latin": -4, "symbol": -4}}}
    for i, p in enumerate(paragraphs):
        p["index"] = i
        p["path"] = "sec0/p%d" % i
        if p["para_format"]["para_pr_id"] is None:
            p["para_format"]["para_pr_id"] = 100 + i   # 문단마다 다른 paraPr (공유 영향 배제)
        cf = p["char_format"]
        if cf["char_pr_id"] >= 100:
            char_props.setdefault(str(cf["char_pr_id"]), {
                "id": cf["char_pr_id"], "font_face": dict(FACES), "size_pt": cf["size_pt"],
                "ratio": {"hangul": 100, "latin": 100}, "spacing": {"hangul": 0, "latin": 0}})
    return {
        "schema_version": "1.0", "paragraphs": paragraphs, "tables": tables or [],
        "format_definitions": {"char_properties": char_props},
        "verification": {"all_ok": True},
        "document": {"sections": []},
        "package": {"version": {"application": app}},
        "source": {"file_name": "synthetic.hwpx"},
    }


def rule_results(paragraphs, rule_id, doc_type=None, config=CONFIG, sub_item=False, **kw):
    res = check_document(inspection(paragraphs, **kw), config, CONFIG_PATH, doc_type=doc_type)
    by_idx = {}
    for r in res["results"]:
        if r["rule_id"] == rule_id and r["target"].get("type") == "paragraph":
            key = (r["target"]["paragraph_index"], r.get("sub_item")) if sub_item else r["target"]["paragraph_index"]
            by_idx[key] = r
    return by_idx, res


def spacing(paragraphs, **kw):
    return rule_results(paragraphs, "PARA_SPACING", **kw)


def expected_of(by_idx, i):
    r = by_idx[i]
    return (r.get("expected") or {}).get("value"), r["status"]


class SpacingTableTest(unittest.TestCase):
    def test_item_above(self):
        self.assertEqual([item_above_spacing(l, SPACING)[0] for l in (1, 2, 3, 4, 5, 6)], [20, 16, 14, 14, 14, 14])

    def test_bullet_above(self):
        b = lambda m, l: {"kind": "bullet", "level": l, "marker": m}  # noqa: E731
        item = {"kind": "item", "level": 1, "marker": "1."}
        self.assertEqual(bullet_above_spacing(b("□", 1), 1, SPACING)[:2], (14, ()))         # □ → □
        self.assertEqual(bullet_above_spacing(b("ㅇ", 2), 1, SPACING)[:2], (14, ()))        # ㅇ → □ 복귀
        self.assertEqual(bullet_above_spacing(item, 1, SPACING)[:2], (14, ()))              # 번호 항목 → □
        self.assertEqual(bullet_above_spacing(None, 1, SPACING)[:2], (14, ()))              # 첫 항목 □
        self.assertEqual(bullet_above_spacing(item, 1, SPACING, after_heading_1=True)[:2], (14, (14, 15)))
        self.assertEqual(bullet_above_spacing(b("□", 1), 2, SPACING)[0], 6)    # □ → ㅇ
        self.assertEqual(bullet_above_spacing(b("ㅇ", 2), 2, SPACING)[0], 6)   # ㅇ → ㅇ
        self.assertEqual(bullet_above_spacing(b("ㅇ", 2), 3, SPACING)[0], 6)   # ㅇ → -
        self.assertEqual(bullet_above_spacing(b("-", 3), 4, SPACING)[0], 6)    # - → ․
        for prev, cur in ((b("-", 3), 3), (b("-", 3), 2), (b("․", 4), 2), (item, 2), (None, 2)):
            self.assertIsNone(bullet_above_spacing(prev, cur, SPACING)[0], (prev, cur))


class RequiredCasesTest(unittest.TestCase):
    """요구사항 7 문단 간격."""

    def check_pair(self, first, second, pt, middle=()):
        paras = [P(first)] + [P(t) for t in middle] + [P(second, space_before=pt)]
        by_idx, _ = spacing(paras)
        last = len(paras) - 1
        self.assertEqual(expected_of(by_idx, last), (pt, "PASS"), "%s → %s: %s" % (first, second, by_idx[last]["reason"]))
        paras = [P(first)] + [P(t) for t in middle] + [P(second, space_before=pt + 3)]
        by_idx, _ = spacing(paras)
        self.assertEqual(by_idx[last]["status"], "FAIL")

    def test_numbered_items_by_own_level(self):
        self.check_pair("1. 사업 개요", "2. 사업 추진 계획", 20)
        self.check_pair("1. 사업 개요", "가. 추진 배경", 16)
        self.check_pair("가. 추진 배경", "나. 추진 목적", 16)
        self.check_pair("가. 추진 배경", "1) 세부 추진 배경", 14)
        self.check_pair("1) 세부 추진 배경", "나. 추진 목적", 16)   # 위 문단과 무관하게 '가.' 단계는 16
        self.check_pair("1) 세부 추진 배경", "2) 추가 추진 사항", 14)
        self.check_pair("가) 세부 사항", "2. 사업 추진 계획", 20)
        self.check_pair("1) 세부", "① 세부의 세부", 14)
        self.check_pair("① 세부", "㉮ 더 세부", 14)
        self.check_pair("1. 사업 개요", "2. 사업 추진 계획", 20, middle=["사업 개요에 대한 설명 본문이다."])

    def test_first_item_is_20(self):
        by_idx, _ = spacing([P("1. 개요", 20)])
        self.assertEqual(expected_of(by_idx, 0), (20, "PASS"))

    def test_body(self):
        paras = [P("1. 사업 개요"), P("첫 번째 설명 본문이다.", 6), P("두 번째 설명 본문이다.", 6)]
        by_idx, _ = spacing(paras)
        self.assertEqual(expected_of(by_idx, 1), (6, "PASS"))   # 항목 → 일반 본문
        self.assertEqual(expected_of(by_idx, 2), (6, "PASS"))   # 일반 본문 → 일반 본문
        self.assertEqual(by_idx[1]["hierarchy"]["owner_item"]["paragraph"], 0)

    def test_bullets(self):
        self.check_pair("□ 첫 항목", "□ 둘째 항목", 14)
        self.check_pair("□ 첫 항목", "□ 둘째 항목", 14, middle=["첫 항목의 설명 본문이다."])   # 본문은 건너뜀
        self.check_pair("ㅇ 세부", "□ 상위로 복귀", 14)
        self.check_pair("- 더 세부", "□ 상위로 복귀", 14)
        self.check_pair("1. 번호 항목", "□ 기호 항목", 14)
        self.check_pair("□ 항목", "ㅇ 세부 항목", 6)
        self.check_pair("ㅇ 하나", "ㅇ 둘", 6)
        self.check_pair("ㅇ 세부 항목", "- 더 세부", 6)
        self.check_pair("- 더 세부", "․ 가장 세부", 6)
        self.check_pair("□ 항목", "ㅇ 세부 항목", 6, middle=["본문 설명이다."])

    def test_square_after_heading_allows_15(self):
        for pt in (14, 15):
            by_idx, _ = spacing([P("1. 대제목", role="heading_1", size=16), P("□ 바로 다음 항목", pt)])
            self.assertEqual(by_idx[1]["status"], "PASS", pt)
            self.assertEqual(by_idx[1]["expected"]["allowed"], [14, 15])
        by_idx, _ = spacing([P("1. 대제목", role="heading_1", size=16), P("□ 바로 다음 항목", 16)])
        self.assertEqual((by_idx[1]["status"], by_idx[1]["fix"]["actions"][0]["to"]), ("FAIL", 14))
        by_idx, _ = spacing([P("1. 대제목", role="heading_1", size=16), P("본문", 6), P("□ 본문 뒤 항목", 15)])
        self.assertEqual(by_idx[2]["status"], "FAIL")   # 대제목 바로 다음이 아니면 14 만

    def test_bullet_transitions_without_standard_are_unknown(self):
        for first, second in (("- 하나", "- 둘"), ("․ 하나", "․ 둘"), ("- 세부", "ㅇ 복귀"), ("1. 번호 항목", "ㅇ 기호 항목"),
                              ("□ 항목", "- 두 층위 아래")):
            by_idx, _ = spacing([P(first), P(second, 6)])
            self.assertEqual(by_idx[1]["status"], "UNKNOWN", (first, second))
            self.assertIn("미정", by_idx[1]["unknown_reason"])
            self.assertFalse(by_idx[1]["fix"]["actions"])

    def test_bullet_after_number_item_does_not_break_number_rule(self):
        paras = [P("가. 중제목"), P("□ 항목", 14), P("□ 다음 항목", 14), P("2. 계획", 20)]
        by_idx, _ = spacing(paras)
        self.assertEqual(expected_of(by_idx, 1), (14, "PASS"))     # 가. → □ 도 14
        self.assertEqual(expected_of(by_idx, 2), (14, "PASS"))     # □ → □
        self.assertEqual(expected_of(by_idx, 3), (20, "PASS"))     # 번호 항목은 자기 단계 기준

    def test_notes(self):
        paras = [P("1. 개요"), P("본문이다.", 6), P("※ 참고 사항", 6), P("* 또 다른 참고", 6), P("다음 본문", 6),
                 P("□ 항목", 6), P("※ 참고", 6), P("□ 다음 항목", 14)]
        by_idx, _ = spacing(paras)
        for i in (2, 3):
            self.assertEqual(expected_of(by_idx, i), (6, "PASS"))
            self.assertEqual(by_idx[i]["hierarchy"]["kind"], "note")
        self.assertEqual(expected_of(by_idx, 4), (6, "PASS"))      # 주석은 층위 판단에서 제외 → 본문은 앞 본문 기준
        self.assertEqual(expected_of(by_idx, 7), (14, "PASS"))     # □ → (주석) → □ 는 □ 사이 14

    def test_unmapped_markers_are_unknown(self):
        for text in ("Ⅰ. 로마 숫자", "(1) 괄호 번호", "■ 검은 사각형", "○ 원 기호"):
            by_idx, _ = spacing([P("1. 개요"), P(text, 14)])
            self.assertEqual(by_idx[1]["status"], "UNKNOWN", text)
            self.assertIn("표준", by_idx[1]["unknown_reason"])

    def test_type_a(self):
        tables = [{"id": "T0", "path": "sec0/p0/tbl0", "context": "body", "nesting_depth": 0, "rows": 1, "cols": 1,
                   "anchor_paragraph": 0, "cells": [{"paragraphs": [1]}]}]
        paras = [P("", objects=["T0"]), P("입 찰 공 고", role="document_title", container="table_cell"),
                 P(""), P("1. 입찰에 부치는 사항", space_before=20)]
        by_idx, res = spacing(paras, doc_type="A", tables=tables)
        self.assertEqual(expected_of(by_idx, 3), (20, "PASS"))
        self.assertIn("TYPE A", by_idx[3]["expected"]["basis"])
        self.assertIn("빈 문단 1개", by_idx[3]["observation"])
        by_idx, _ = spacing(copy.deepcopy(paras), tables=tables)   # 유형 미지정이면 특례를 추측하지 않음
        self.assertEqual(by_idx[3]["status"], "UNKNOWN")
        self.assertEqual(by_idx[3]["sub_item"], "TYPE A/B 특례")

    def test_type_b(self):
        paras = [P("1. 개요\t1", role="toc"), P("2. 계획\t3", role="toc"),
                 P("1. 사업 개요", space_before=0, page_break=True), P("가. 추진 배경", 16)]
        by_idx, _ = spacing(paras, doc_type="B")
        self.assertEqual(expected_of(by_idx, 2), (0, "PASS"))
        self.assertEqual(expected_of(by_idx, 3), (16, "PASS"))
        paras[2]["page_break_before"] = False
        by_idx, _ = spacing(paras, doc_type="B")
        self.assertEqual(by_idx[2]["status"], "UNKNOWN")

    def test_idempotent_absolute_target(self):
        paras = [P("1. 사업 개요"), P("2. 사업 추진 계획", space_before=30)]
        by_idx, res = spacing(paras)
        a = by_idx[1]["fix"]["actions"][0]
        self.assertEqual((a["op"], a["from"], a["to"]), ("set_para_space_before", 30, 20))
        by_idx2, res2 = spacing([P("1. 사업 개요", 20), P("2. 사업 추진 계획", space_before=20)])
        self.assertEqual(by_idx2[1]["status"], "PASS")
        self.assertEqual([x for x in res2["fix_plan"]["role_based_actions"] if x["op"] == "set_para_space_before"], [])

    def test_existing_blank_paragraph_is_kept_and_reported(self):
        paras = [P("1. 사업 개요"), P(""), P("2. 사업 추진 계획", 20)]
        by_idx, res = spacing(paras)
        self.assertEqual(by_idx[2]["status"], "PASS")
        self.assertIn("빈 문단 1개", by_idx[2]["observation"])
        self.assertIn("삭제하지 않음", by_idx[2]["observation"])

    def test_previous_space_after_reported(self):
        by_idx, _ = spacing([P("1. 개요", space_after=10), P("2. 계획", 20)])
        self.assertIn("문단 아래 간격 10pt", by_idx[1]["observation"])

    def test_auto_numbering_level(self):
        an = {"type": "OUTLINE", "level": 1, "num_format": "DIGIT", "format_text": "^1."}
        an2 = {"type": "OUTLINE", "level": 2, "num_format": "HANGUL_SYLLABLE", "format_text": "^2."}
        paras = [P("사업 개요", 20, auto_numbering=an, role="heading_1", size=16),
                 P("추진 배경", 16, auto_numbering=an2, role="heading_2", size=15)]
        annotate_hierarchy(inspection(paras)["paragraphs"])
        self.assertEqual((paras[0]["hierarchy"]["level"], paras[1]["hierarchy"]["level"]), (1, 2))
        by_idx, _ = spacing(paras)
        self.assertEqual(expected_of(by_idx, 1), (16, "PASS"))

    def test_other_paragraph_breaks_chain(self):
        paras = [P("ㅇ 항목"), P("<표 1> 실적", role="table_title"), P("ㅇ 다음 항목", 6)]
        by_idx, res = spacing(paras)
        self.assertEqual(by_idx[2]["status"], "UNKNOWN")      # 직전 항목을 알 수 없음 (□ 는 앞과 무관하게 14)
        self.assertTrue(any(r["status"] == "SKIP" for r in res["results"] if r["rule_id"] == "PARA_SPACING"))

    def test_invalid_doc_type(self):
        with self.assertRaises(CheckerError):
            check_document(inspection([P("1. 개요")]), CONFIG, doc_type="C")

    def test_input_not_mutated(self):
        ins = inspection([P("1. 개요"), P("2. 계획", 20)])
        before = copy.deepcopy(ins)
        check_document(ins, CONFIG)
        self.assertEqual(ins, before)


def hanging(paragraphs, app="Hancom Office Hangul"):
    return rule_results(paragraphs, "HANGING_INDENT", sub_item=True, app=app)


class HangingIndentTest(unittest.TestCase):
    def test_marker_space(self):
        out, _ = hanging([P("□ 한 칸 공백"), P("□  두 칸 공백"), P("1.붙여 씀"), P("가.\t탭"), P("※ 주석"), P("*  주석 두 칸")])
        self.assertEqual(out[(0, "번호·기호 뒤 공백")]["status"], "PASS")
        self.assertEqual(out[(4, "번호·기호 뒤 공백")]["status"], "PASS")
        for i in (1, 2, 3, 5):
            r = out[(i, "번호·기호 뒤 공백")]
            self.assertEqual(r["status"], "FAIL")
            self.assertFalse(r["fix"]["possible"])               # 글자·공백 변경 금지 → 자동 수정 안 함
            self.assertEqual(r["fix"]["mode"], "manual_review")

    def test_leading_spaces_observed(self):
        out, _ = hanging([P("  □ 앞 공백")])
        self.assertIn("앞에 공백 2칸", out[(0, "번호·기호 뒤 공백")]["observation"])

    def _align(self, paras, i=0, **kw):
        out, res = hanging(paras, **kw)
        return out[(i, "둘째 줄 정렬")], res

    def test_width_estimate_by_char_count(self):
        # 14pt 기준: 한글·기호·※ 1칸 = 14pt, 공백·숫자·괄호·마침표·* 0.5칸 = 7pt
        # '1.'/'가.' 는 대제목 16pt/중제목 15pt 로 바뀔 예정이므로 그 크기로 계산한다
        cases = [("□ 항목", 21.0), ("  □ 앞 공백", 35.0), ("ㅇ 세부", 21.0), ("- 하위", 14.0),
                 ("1) 번호", 21.0), ("가. 번호", 30.0), ("1. 번호", 24.0), ("※ 주석", 21.0), ("* 주석", 14.0)]
        for text, width in cases:
            r, _ = self._align([P(text, indent=0)])
            self.assertEqual(r["expected"]["value"]["indent_pt"], -width, text)
            self.assertEqual(r["status"], "FAIL", text)

    def test_fail_has_auto_fix_action(self):
        r, res = self._align([P("□ 내어쓰기 없는 문단", indent=0)])
        self.assertTrue(r["fix"]["possible"])
        a = r["fix"]["actions"][0]
        self.assertEqual((a["op"], a["from"], a["to"]), ("set_para_hanging_indent", 0, -21.0))
        self.assertTrue(any(x["op"] == "set_para_hanging_indent" for x in res["fix_plan"]["role_based_actions"]))

    def test_tolerance_half_point(self):
        r, _ = self._align([P("□ 거의 맞는 내어쓰기", indent=-21.4)])
        self.assertEqual(r["status"], "PASS")
        r, _ = self._align([P("□ 조금 다른 내어쓰기", indent=-21.6)])
        self.assertEqual(r["status"], "FAIL")
        self.assertIn("±0.5pt", r["reason"])

    def test_table_cell_paragraph_is_checked(self):
        r, _ = self._align([P("□ 표 안 항목", container="table_cell", role="table_cell")])
        self.assertEqual(r["status"], "FAIL")
        self.assertEqual(r["expected"]["value"]["indent_pt"], -21.0)

    def test_checked_regardless_of_lines_or_layout(self):
        r, _ = self._align([P("□ 한 줄 문단", lines=1)])
        self.assertEqual(r["status"], "FAIL")
        p = P("□ 줄 배치 없는 문단")
        p["line_layout"] = None
        r, _ = self._align([p])
        self.assertEqual(r["status"], "FAIL")
        r, _ = self._align([P("□ 다른 프로그램", lines=2)], app="Other Writer")
        self.assertEqual(r["status"], "FAIL")

    def test_char_spacing_is_actual_not_standard(self):
        p = P("□ 자간 -4% 문단")
        p["runs"] = [{"char_pr_id": 2, "chars": len(p["text"])}]
        p["char_format"]["char_pr_id"] = 2
        r, _ = self._align([p])
        # 자간은 수정하지 않으므로 -4% 그대로(글자마다 크기 × -4% 를 더함): (14 - 0.56) + (7 - 0.56) = 19.88
        self.assertEqual(r["expected"]["value"]["indent_pt"], -19.88)

    def test_width_uses_font_size_after_fix(self):
        # 대제목 '1. 개요' 가 14pt → 16pt 로 바뀔 예정이면 내어쓰기 폭도 16pt 기준(1. = 8+8+8)
        r, res = self._align([P("1. 개요", size=14)])
        self.assertEqual(r["expected"]["value"]["indent_pt"], -24.0)
        self.assertTrue(any(x["op"] == "set_char_size" for x in res["fix_plan"]["role_based_actions"]))

    def test_whitespace_run_uses_its_own_format(self):
        p = P("  □ 앞 공백은 10pt 자간 -10%")
        p["runs"] = [{"char_pr_id": 1, "chars": 2}, {"char_pr_id": 0, "chars": len(p["text"]) - 2}]
        p["char_format_mixed_detail"] = [{"char_pr_ids": [0]}]
        r, _ = self._align([p])
        self.assertEqual(r["expected"]["value"]["indent_pt"], -29.0)   # 공백 2개 × (5 - 1) = 8, □ 14, 공백 7

    def test_tab_or_unknown_char_is_unknown(self):
        r, _ = self._align([P("□\t탭 뒤 본문")])
        self.assertEqual(r["status"], "UNKNOWN")
        self.assertIn("U+0009", r["unknown_reason"])

    def test_auto_numbered_paragraph_is_checked_but_unknown(self):
        an = {"type": "NUMBER", "level": 0, "num_format": "DIGIT", "format_text": "^1."}
        out, _ = hanging([P("자동 번호 두 줄 문단", lines=2, auto_numbering=an)])
        self.assertEqual(out[(0, "둘째 줄 정렬")]["status"], "UNKNOWN")
        self.assertNotIn((0, "번호·기호 뒤 공백"), out)


def font_size(paragraphs):
    return rule_results(paragraphs, "FONT_SIZE")


class FontSizeTest(unittest.TestCase):
    def test_headings(self):
        by_idx, res = font_size([P("1. 대제목", size=14), P("가. 중제목", size=15)])
        self.assertEqual((by_idx[0]["status"], by_idx[0]["expected"]["value"]), ("FAIL", 16))
        a = by_idx[0]["fix"]["actions"][0]
        self.assertEqual((a["op"], a["from"], a["to"]), ("set_char_size", 14.0, 16))
        self.assertEqual(by_idx[1]["status"], "PASS")
        self.assertTrue(any(x["op"] == "set_char_size" and x["to"] == 16 for x in res["fix_plan"]["role_based_actions"]))

    def test_number_defines_heading_role(self):
        # '1.'/'가.' 는 분류기 역할·신뢰도와 무관하게 대제목/중제목 기준(요구사항 3 용어 정의)
        by_idx, res = font_size([P("1. 대제목", size=14, confidence=0.4), P("가. 중제목", role="unclassified", size=14)])
        self.assertEqual((by_idx[0]["status"], by_idx[0]["expected"]["value"]), ("FAIL", 16))
        self.assertEqual((by_idx[1]["status"], by_idx[1]["expected"]["value"]), ("FAIL", 15))
        self.assertEqual(by_idx[0]["role"]["label"], "대제목")
        font = {r["target"]["paragraph_index"]: r for r in res["results"] if r["rule_id"] == "FONT" and r["target"].get("type") == "paragraph"}
        self.assertEqual(font[1]["expected"]["value"], "HY헤드라인M")

    def test_heading_without_number_is_unknown(self):
        for conf in (0.4, 0.95):
            by_idx, _ = font_size([P("번호 없는 제목", role="heading_1", size=14, confidence=conf)])
            self.assertEqual(by_idx[0]["status"], "UNKNOWN")
            self.assertIn("번호 없이", by_idx[0]["unknown_reason"])

    def test_note_is_two_points_smaller_than_previous(self):
        by_idx, _ = font_size([P("본문 문장이다.", size=14), P("※ 주석", size=12), P("* 둘째 주석", size=12), P("※ 큰 주석", size=14)])
        self.assertEqual((by_idx[1]["status"], by_idx[1]["expected"]["value"]), ("PASS", 12))
        self.assertEqual((by_idx[2]["status"], by_idx[2]["expected"]["value"]), ("PASS", 12))   # 연속 주석은 같은 본문 기준
        self.assertEqual((by_idx[3]["status"], by_idx[3]["expected"]["value"]), ("FAIL", 12))
        by_idx, _ = font_size([P("※ 앞 문단 없는 주석", size=12)])
        self.assertEqual(by_idx[0]["status"], "UNKNOWN")

    def test_note_under_table_is_excluded(self):
        paras = [P("본문 문장이다.", size=14), P("", objects=["T0"]), P("※ 표 아래 주석", size=14), P("※ 둘째 주석", size=14)]
        by_idx, res = font_size(paras)
        self.assertNotIn(2, by_idx)                                   # 표 바로 아래 주석은 제외(SKIP 묶음)
        self.assertEqual((by_idx[3]["status"], by_idx[3]["expected"]["value"]), ("FAIL", 12))   # 그다음 주석은 본문 기준
        skips = [r for r in res["results"] if r["rule_id"] == "FONT_SIZE" and r["status"] == "SKIP"]
        self.assertTrue(any(2 in r["target"]["paragraph_indices"] and "표 바로 아래" in r["unknown_reason"] for r in skips))
        paras = [P("본문 문장이다.", size=14), P("", objects=["P0"]), P("※ 그림 아래 주석", size=14)]
        by_idx, _ = font_size(paras)
        self.assertEqual(by_idx[2]["status"], "FAIL")                 # 그림 아래는 제외 대상 아님

    def test_square_bullet_14_or_15_after_heading(self):
        by_idx, _ = font_size([P("1. 대제목", role="heading_1", size=16), P("□ 바로 다음 항목", size=15),
                               P("본문", size=14), P("□ 본문 뒤 항목", size=15), P("□ 작은 항목", size=13), P("□ 표준", size=14)])
        self.assertEqual(by_idx[1]["status"], "PASS")
        self.assertIn("15pt 허용", by_idx[1]["expected"]["basis"])
        self.assertEqual((by_idx[3]["status"], by_idx[3]["expected"]["value"]), ("FAIL", 14))
        self.assertEqual(by_idx[4]["status"], "FAIL")
        self.assertEqual(by_idx[5]["status"], "PASS")

    def test_toc(self):
        by_idx, _ = font_size([P("목 차", role="toc", size=16), P("1. 개요\t1", role="toc", size=14), P("붙임\t9", role="toc", size=12)])
        self.assertEqual([by_idx[i]["status"] for i in (0, 1, 2)], ["PASS", "PASS", "FAIL"])
        self.assertEqual(by_idx[2]["expected"]["value"], 14)

    def test_undecided_and_unspecified(self):
        by_idx, res = font_size([P("보고서 제목", role="document_title", size=22), P("일반 본문", size=14)])
        self.assertEqual(by_idx[0]["status"], "UNKNOWN")
        self.assertIn("표지", by_idx[0]["unknown_reason"])
        self.assertNotIn(1, by_idx)   # 본문은 표준 미정 → 묶음 SKIP
        skips = [r for r in res["results"] if r["rule_id"] == "FONT_SIZE" and r["status"] == "SKIP"]
        self.assertTrue(any(1 in r["target"]["paragraph_indices"] for r in skips))


if __name__ == "__main__":
    unittest.main()

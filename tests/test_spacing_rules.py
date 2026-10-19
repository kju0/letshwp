"""개정 FORMAT_SPEC 신규 규칙 테스트.

- 5장 계층별 문단 간격(5.9 필수 테스트 사례 포함)과 일반 본문 소속
- 5.6 TYPE A / TYPE B 특례
- 4장 항목 번호·기호의 줄바꿈 정렬(내어쓰기)

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
from hwpx_analysis.hierarchy import annotate_hierarchy, level_spacing  # noqa: E402

CONFIG, CONFIG_PATH = load_config()
SPACING = CONFIG["rules"]["PARA_SPACING"]


def P(text, space_before=0.0, role=None, indent=0.0, lines=1, page_break=False, objects=None,
      container="body", space_after=0.0, auto_numbering=None, para_pr_id=None):
    """간이 문단. role 을 생략하면 번호가 있으면 본문(항목), 없으면 본문으로 둔다."""
    empty = not text.strip()
    role = role or ("object_anchor" if empty and objects else "empty" if empty else "body")
    return {
        "text": text, "is_empty": empty, "visible_length": len(text.strip()),
        "context": {"container": container, "depth": 0 if container == "body" else 1,
                    "table_id": "T9" if container == "table_cell" else None,
                    "cell": [0, 0] if container == "table_cell" else None, "object_id": None},
        "role": {"role": role, "label": ROLE_LABELS[role], "confidence": 0.9, "evidence": [{"rule": "test"}]},
        "list_marker": detect_list_marker(text),
        "auto_numbering": auto_numbering,
        "objects": objects or [],
        "page_break_before": page_break,
        "para_format": {"para_pr_id": para_pr_id, "space_before_pt": space_before, "space_after_pt": space_after,
                        "indent_pt": indent, "left_margin_pt": 0.0, "align": "JUSTIFY",
                        "line_spacing_type": "PERCENT", "line_spacing_value": 160, "line_spacing": "160%"},
        "char_format": {"char_pr_id": 0, "font_hangul": "휴먼명조", "size_pt": 14.0},
        "runs": [{"char_pr_id": 0, "chars": len(text)}],
        "line_layout": {"line_count": lines, "lines": [{"textpos": 0, "horzpos_pt": 0.0, "horzsize_pt": 480.0}] * lines,
                        "source": "test"},
    }


def inspection(paragraphs, app="Hancom Office Hangul", tables=None):
    for i, p in enumerate(paragraphs):
        p["index"] = i
        p["path"] = "sec0/p%d" % i
        if p["para_format"]["para_pr_id"] is None:
            p["para_format"]["para_pr_id"] = 100 + i   # 문단마다 다른 paraPr (공유 영향 배제)
    return {
        "schema_version": "1.0", "paragraphs": paragraphs, "tables": tables or [],
        "format_definitions": {"char_properties": {"0": {"id": 0, "font_face": {"hangul": "휴먼명조"},
                                                         "ratio": {"hangul": 100, "latin": 100},
                                                         "spacing": {"hangul": 0, "latin": 0}}}},
        "verification": {"all_ok": True},
        "document": {"sections": []},
        "package": {"version": {"application": app}},
        "source": {"file_name": "synthetic.hwpx"},
    }


def spacing(paragraphs, doc_type=None, config=CONFIG, **kw):
    res = check_document(inspection(paragraphs, **kw), config, CONFIG_PATH, doc_type=doc_type)
    by_idx = {}
    for r in res["results"]:
        if r["rule_id"] == "PARA_SPACING" and r["target"].get("type") == "paragraph":
            by_idx[r["target"]["paragraph_index"]] = r
    return by_idx, res


def expected_of(by_idx, i):
    r = by_idx[i]
    return (r.get("expected") or {}).get("value"), r["status"]


class LevelSpacingTableTest(unittest.TestCase):
    """5.2 계층 이동별 적용 간격 표."""

    def test_table(self):
        cases = {(1, 1): 16, (1, 2): 16, (2, 1): 16, (1, 5): 16, (6, 1): 16, (2, 2): 15, (2, 3): 14, (3, 2): 14,
                 (2, 4): 15, (6, 2): 15, (3, 3): 14, (3, 6): 14, (4, 5): 14, (6, 4): 14}
        for (a, b), v in cases.items():
            self.assertEqual(level_spacing(a, b, SPACING)[0], v, (a, b))


class RequiredCasesTest(unittest.TestCase):
    """FORMAT_SPEC 5.9 필수 테스트 사례."""

    def check_pair(self, first, second, pt, middle=()):
        paras = [P(first)] + [P(t) for t in middle] + [P(second, space_before=pt)]
        by_idx, _ = spacing(paras)
        last = len(paras) - 1
        self.assertEqual(expected_of(by_idx, last), (pt, "PASS"), "%s → %s" % (first, second))
        # 다른 값이면 FAIL
        paras = [P(first)] + [P(t) for t in middle] + [P(second, space_before=pt + 3)]
        by_idx, _ = spacing(paras)
        self.assertEqual(by_idx[last]["status"], "FAIL")

    def test_1_to_2(self):
        self.check_pair("1. 사업 개요", "2. 사업 추진 계획", 16)

    def test_1_to_ga(self):
        self.check_pair("1. 사업 개요", "가. 추진 배경", 16)

    def test_ga_to_na(self):
        self.check_pair("가. 추진 배경", "나. 추진 목적", 15)

    def test_ga_to_1paren(self):
        self.check_pair("가. 추진 배경", "1) 세부 추진 배경", 14)

    def test_1paren_to_na(self):
        self.check_pair("1) 세부 추진 배경", "나. 추진 목적", 14)

    def test_1paren_to_2paren(self):
        self.check_pair("1) 세부 추진 배경", "2) 추가 추진 사항", 14)

    def test_gaparen_to_2(self):
        self.check_pair("가) 세부 사항", "2. 사업 추진 계획", 16)

    def test_body_under_1_then_2(self):
        self.check_pair("1. 사업 개요", "2. 사업 추진 계획", 16, middle=["사업 개요에 대한 설명 본문이다."])

    def test_body_under_ga_then_na(self):
        self.check_pair("가. 추진 배경", "나. 추진 목적", 15, middle=["추진 배경 설명 본문이다."])

    def test_body_to_body(self):
        paras = [P("1. 사업 개요"), P("첫 번째 설명 본문이다.", 6), P("두 번째 설명 본문이다.", 6)]
        by_idx, _ = spacing(paras)
        self.assertEqual(expected_of(by_idx, 1), (6, "PASS"))   # 항목 → 일반 본문
        self.assertEqual(expected_of(by_idx, 2), (6, "PASS"))   # 일반 본문 → 일반 본문

    def test_example_5_5(self):
        """5.5 적용 예시 전체 흐름."""
        paras = [P("1. 사업 개요"), P("가. 추진 배경", 16), P("1) 세부 추진 배경", 14),
                 P("세부 설명 본문이다.", 6), P("2) 추가 추진 사항", 14), P("나. 추진 목적", 14),
                 P("추진 목적 설명이다.", 6), P("2. 사업 추진 계획", 16)]
        by_idx, _ = spacing(paras)
        for i in range(1, len(paras)):
            self.assertEqual(by_idx[i]["status"], "PASS", (i, by_idx[i]["reason"]))
        self.assertEqual(by_idx[3]["hierarchy"]["owner_item"]["paragraph"], 2)   # 본문은 1) 소속
        self.assertEqual(by_idx[4]["hierarchy"]["compare_with"]["paragraph"], 2)  # 본문이 아니라 소속 항목과 비교
        self.assertIn("interpretation_note", by_idx[5])                          # 1) → 나. 은 6장 미확정 해석

    def test_type_a(self):
        tables = [{"id": "T0", "path": "sec0/p0/tbl0", "context": "body", "nesting_depth": 0, "rows": 1, "cols": 1,
                   "anchor_paragraph": 0, "cells": [{"paragraphs": [1]}]}]
        paras = [P("", objects=["T0"]), P("입 찰 공 고", role="document_title", container="table_cell"),
                 P(""), P("1. 입찰에 부치는 사항", space_before=20)]
        by_idx, res = spacing(paras, doc_type="A", tables=tables)
        self.assertEqual(expected_of(by_idx, 3), (20, "PASS"))
        self.assertIn("빈 문단 1개", by_idx[3]["observation"])
        self.assertEqual(res["document"]["document_type"], {"value": "A", "source": "CLI 지정",
                                                           "note": "TYPE A/B 판별 기준 미확정 - 자동 판별하지 않음"})
        # 유형 미지정이면 특례를 추측하지 않음
        by_idx, _ = spacing(copy.deepcopy(paras), tables=tables)
        self.assertEqual(by_idx[3]["status"], "UNKNOWN")
        self.assertEqual(by_idx[3]["sub_item"], "TYPE A/B 특례")

    def test_type_b(self):
        paras = [P("1. 개요\t1", role="toc"), P("2. 계획\t3", role="toc"),
                 P("1. 사업 개요", space_before=0, page_break=True), P("가. 추진 배경", 16)]
        by_idx, _ = spacing(paras, doc_type="B")
        self.assertEqual(expected_of(by_idx, 2), (0, "PASS"))
        self.assertEqual(expected_of(by_idx, 3), (16, "PASS"))   # 이후는 일반 규칙
        # 쪽 나눔이 확인되지 않으면 판단 불가
        paras[2]["page_break_before"] = False
        by_idx, _ = spacing(paras, doc_type="B")
        self.assertEqual(by_idx[2]["status"], "UNKNOWN")

    def test_idempotent_absolute_target(self):
        """2회 변환 누적 없음: 수정 동작은 증분이 아니라 목표값(set)이며, 목표값에 도달한 문서는 다시 수정 대상이 아님."""
        paras = [P("1. 사업 개요"), P("2. 사업 추진 계획", space_before=30)]
        by_idx, res = spacing(paras)
        a = by_idx[1]["fix"]["actions"][0]
        self.assertEqual((a["op"], a["from"], a["to"]), ("set_para_space_before", 30, 16))
        # 목표값을 적용한 상태(16pt)를 다시 검사하면 PASS 이고 수정 동작이 없음
        paras2 = [P("1. 사업 개요"), P("2. 사업 추진 계획", space_before=16)]
        by_idx2, res2 = spacing(paras2)
        self.assertEqual(by_idx2[1]["status"], "PASS")
        self.assertEqual([x for x in res2["fix_plan"]["role_based_actions"] if x["op"] == "set_para_space_before"], [])

    def test_existing_blank_paragraph_is_kept_and_reported(self):
        paras = [P("1. 사업 개요"), P(""), P("2. 사업 추진 계획", 16)]
        by_idx, res = spacing(paras)
        self.assertEqual(by_idx[2]["status"], "PASS")
        self.assertIn("빈 문단 1개", by_idx[2]["observation"])
        self.assertIn("삭제하지 않음", by_idx[2]["observation"])
        self.assertTrue(any("빈 문단" in x for x in res["fix_plan"]["preservation_rules"]))


class AmbiguityTest(unittest.TestCase):
    def test_unmapped_bullet_is_unknown(self):
        paras = [P("1. 개요"), P("□ 세부 내용", 16), P("2. 계획", 16)]
        by_idx, _ = spacing(paras)
        self.assertEqual(by_idx[1]["status"], "UNKNOWN")
        self.assertIn("'□'", by_idx[1]["unknown_reason"])
        self.assertEqual(by_idx[2]["status"], "UNKNOWN")     # 모호한 문단 뒤 항목은 비교 대상 없음

    def test_bullet_levels_config(self):
        cfg = copy.deepcopy(CONFIG)
        cfg["rules"]["PARA_SPACING"]["bullet_levels"] = {"□": 2}
        paras = [P("1. 개요"), P("□ 세부 내용", 16), P("2. 계획", 16)]
        by_idx, _ = spacing(paras, config=cfg)
        self.assertEqual(expected_of(by_idx, 1), (16, "PASS"))
        self.assertEqual(expected_of(by_idx, 2), (16, "PASS"))

    def test_note_breaks_ownership(self):
        paras = [P("가. 배경"), P("※ 참고 사항", role="note"), P("설명 본문이다.", 6)]
        by_idx, _ = spacing(paras)
        self.assertEqual(by_idx[2]["status"], "UNKNOWN")
        self.assertTrue(any(r["status"] == "SKIP" for r in spacing(paras)[1]["results"] if r["rule_id"] == "PARA_SPACING"))

    def test_first_item_without_type_is_unknown(self):
        by_idx, _ = spacing([P("1. 개요", 0)])
        self.assertEqual(by_idx[0]["status"], "UNKNOWN")

    def test_previous_space_after_reported(self):
        paras = [P("1. 개요", space_after=10), P("2. 계획", 16)]
        by_idx, _ = spacing(paras)
        self.assertIn("문단 아래 간격 10pt", by_idx[1]["observation"])

    def test_auto_numbering_level(self):
        an = {"type": "OUTLINE", "level": 1, "num_format": "DIGIT", "format_text": "^1."}
        an2 = {"type": "OUTLINE", "level": 2, "num_format": "HANGUL_SYLLABLE", "format_text": "^2."}
        paras = [P("사업 개요", auto_numbering=an, role="heading_1"), P("추진 배경", 16, auto_numbering=an2, role="heading_2")]
        annotate_hierarchy(inspection(paras)["paragraphs"])
        self.assertEqual((paras[0]["hierarchy"]["level"], paras[1]["hierarchy"]["level"]), (1, 2))
        by_idx, _ = spacing(paras)
        self.assertEqual(expected_of(by_idx, 1), (16, "PASS"))

    def test_invalid_doc_type(self):
        with self.assertRaises(CheckerError):
            check_document(inspection([P("1. 개요")]), CONFIG, doc_type="C")

    def test_input_not_mutated(self):
        ins = inspection([P("1. 개요"), P("2. 계획", 16)])
        before = copy.deepcopy(ins)
        check_document(ins, CONFIG)
        self.assertEqual(ins, before)


def hanging(paragraphs, app="Hancom Office Hangul"):
    res = check_document(inspection(paragraphs, app=app), CONFIG, CONFIG_PATH)
    out = {}
    for r in res["results"]:
        if r["rule_id"] == "HANGING_INDENT" and r["target"].get("type") == "paragraph":
            out[(r["target"]["paragraph_index"], r.get("sub_item"))] = r
    return out, res


class HangingIndentTest(unittest.TestCase):
    def test_marker_space(self):
        out, _ = hanging([P("□ 한 칸 공백"), P("□  두 칸 공백"), P("1.붙여 씀"), P("가.\t탭")])
        self.assertEqual(out[(0, "번호·기호 뒤 공백")]["status"], "PASS")
        for i in (1, 2, 3):
            r = out[(i, "번호·기호 뒤 공백")]
            self.assertEqual(r["status"], "FAIL")
            self.assertFalse(r["fix"]["possible"])               # 글자·공백 변경 금지 → 자동 수정 안 함
            self.assertEqual(r["fix"]["mode"], "manual_review")

    def test_leading_spaces_observed(self):
        out, _ = hanging([P("  □ 앞 공백")])
        self.assertIn("앞에 공백 2칸", out[(0, "번호·기호 뒤 공백")]["observation"])

    def test_multiline_without_hanging_fails(self):
        out, res = hanging([P("□ 두 줄로 넘어가는 긴 항목 문단", indent=0, lines=2),
                            P("□ 들여쓰기된 두 줄 문단", indent=10, lines=2)])
        for i in (0, 1):
            r = out[(i, "둘째 줄 정렬")]
            self.assertEqual(r["status"], "FAIL")
            self.assertFalse(r["fix"]["possible"])
            self.assertIn("글꼴 너비", r["fix"]["blocked_reason"])
        self.assertTrue(any(m["rule_id"] == "HANGING_INDENT" for m in res["fix_plan"]["manual_review"]))

    def test_hanging_present_is_unknown(self):
        out, _ = hanging([P("□ 내어쓰기가 있는 두 줄 문단", indent=-28.0, lines=2)])
        r = out[(0, "둘째 줄 정렬")]
        self.assertEqual(r["status"], "UNKNOWN")
        self.assertIn("2.0자 폭", r["observation"])

    def test_single_line_skipped(self):
        out, res = hanging([P("□ 한 줄 문단", lines=1)])
        self.assertNotIn((0, "둘째 줄 정렬"), out)
        self.assertTrue(any(r["status"] == "SKIP" and r["rule_id"] == "HANGING_INDENT" for r in res["results"]))

    def test_untrusted_layout_or_missing(self):
        out, _ = hanging([P("□ 두 줄 문단", lines=2)], app="Other Writer")
        self.assertEqual(out[(0, "둘째 줄 정렬")]["status"], "UNKNOWN")
        p = P("□ 두 줄 문단", lines=2)
        p["line_layout"] = None
        out, _ = hanging([p])
        self.assertIn("줄 배치 정보 없음", out[(0, "둘째 줄 정렬")]["reason"])

    def test_auto_numbered_paragraph_is_checked_but_unknown(self):
        an = {"type": "NUMBER", "level": 0, "num_format": "DIGIT", "format_text": "^1."}
        out, _ = hanging([P("자동 번호 두 줄 문단", lines=2, auto_numbering=an)])
        self.assertEqual(out[(0, "둘째 줄 정렬")]["status"], "UNKNOWN")
        self.assertNotIn((0, "번호·기호 뒤 공백"), out)       # 번호가 텍스트에 없음


if __name__ == "__main__":
    unittest.main()

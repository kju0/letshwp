"""표준 서식 검사 규칙.

각 규칙은 `rule(doc) -> list[dict]` 형태의 독립 함수이며 RULES 레지스트리에 등록한다.
규칙을 추가·제거하려면 함수를 만들고 RULES 에 넣거나 빼면 된다.
표준값은 코드가 아니라 config/format_standard.json 에서 읽는다.

판정 원칙
- PASS/FAIL 은 현재값(HWPX 에서 추출한 실제 값)과 표준값을 실제로 비교했을 때만 낸다.
- 데이터가 없거나, 역할이 불확실하거나, 표준값이 미정이면 UNKNOWN.
- 현재 단계의 검사 범위 밖이면 SKIP.
- FORMAT_SPEC 의 '분석만' 항목은 불일치가 관찰되어도 FAIL 로 확정하지 않는다(UNKNOWN + observation).
- '검사만' 항목(날짜·시간·번호·기호·제목 박스)은 FAIL 이어도 자동 수정 대상으로 지정하지 않는다.
- 역할은 Inspector 의 규칙 기반 추정값(role.source)이고, 서식값은 HWPX XML 추출값(current.source)이다.
"""

import copy
import re
from pathlib import Path
from collections import Counter, OrderedDict, defaultdict

from hwpx_analysis.hierarchy import (annotate_hierarchy, item_above_spacing, bullet_above_spacing, detect_bullet,
                                     note_marker_of, LEVEL_EXAMPLE)
from hwpx_analysis.fontmetrics import FontRegistry, default_font_dir

PASS, FAIL, UNKNOWN, SKIP = "PASS", "FAIL", "UNKNOWN", "SKIP"

FIX_MODE_BY_SPEC = {
    "자동 수정": "auto",
    "분석 후 수정": "auto_if_role_confirmed",
    "검사만": "manual_review",
    "분석만": "none",
    "수정 안 함": "none",
}

ROLE_SOURCE = "inspector 추정(규칙 기반 역할 분류)"
VALUE_SOURCE = "HWPX XML 추출값"
EXCLUDED_ROLES = {"empty", "object_anchor"}       # 텍스트가 없어 서식 검사 대상이 아님
STRUCTURAL_SKIP_ROLES = {
    "header_footer": "머리말·꼬리말은 현재 명세의 역할 구분에 포함되지 않음",
    "toc": "차례 항목은 현재 명세의 역할 구분에 포함되지 않음",
}
COMPARED_LANGS = ("hangul", "latin")             # 장평·자간 비교 언어 슬롯(한글 본문, 숫자·영문)
ALIGN_LABEL = {"JUSTIFY": "양쪽 정렬", "LEFT": "왼쪽 정렬", "RIGHT": "오른쪽 정렬", "CENTER": "가운데 정렬",
               "DISTRIBUTE": "배분 정렬", "DISTRIBUTE_SPACE": "나눔 정렬"}
LINE_TYPE_LABEL = {"PERCENT": "글자에 따라", "FIXED": "고정 값", "BETWEEN_LINES": "여백만 지정", "AT_LEAST": "최소"}


# ----------------------------------------------------------------------------
# 문서 접근 도우미
# ----------------------------------------------------------------------------
class Doc:
    """Inspector 결과(dict)와 표준 설정(dict)을 묶은 읽기 전용 접근자."""

    def __init__(self, inspection, config, doc_type=None):
        self.r = inspection
        self.cfg = config
        # 계층 정보를 설정(기호 층위·주석 기호)에 맞춰 다시 계산하므로 입력을 변경하지 않도록 복사해서 쓴다
        self.paragraphs = copy.deepcopy(inspection.get("paragraphs") or [])
        sp = config["rules"].get("PARA_SPACING") or {}
        self.hierarchy_summary = annotate_hierarchy(self.paragraphs, sp.get("bullet_order"), sp.get("note_markers"))
        # 요구사항 3 용어 정의: '1. 2. …'로 시작하면 대제목, '가. 나. …'로 시작하면 중제목.
        # 분류기의 heading_1/2 는 문서 안 번호 순위로 정해지므로(Ⅰ. 가 있으면 1. 이 중제목이 됨) 번호 기준 역할을 따로 둔다.
        for p in self.paragraphs:
            h = p.get("hierarchy") or {}
            p["spec_role"] = {1: "heading_1", 2: "heading_2"}.get(h.get("level")) if h.get("kind") == "item" else None
        self.doc_type = doc_type if doc_type is not None else config.get("document_type")
        self.doc_type_source = ("CLI 지정" if doc_type is not None else
                                "설정 파일" if config.get("document_type") else "미지정")
        self.tables = {t["id"]: t for t in inspection.get("tables") or []}
        fd = inspection.get("format_definitions") or {}
        self.char_props = fd.get("char_properties") or {}
        self.para_props = fd.get("para_properties") or {}
        self.fonts = fd.get("fonts") or {}
        self.sections = (inspection.get("document") or {}).get("sections") or []
        self.role_min = config.get("role_confidence_min", 0.6)
        # FONT_SIZE 규칙이 채우는 '수정 후 글자 크기' (문단 index -> {charPr id: pt}).
        # 내어쓰기 폭 추정은 글자 크기가 바뀔 문단에서 수정 후 크기로 계산해야 하므로 RULES 순서상 FONT_SIZE 가 먼저 돈다.
        self.font_size_targets = {}
        # ITEM_NUMBER/ITEM_BULLET 규칙이 채우는 '교정 후 번호·기호' (문단 index -> (현재, 교정)). HANGING_INDENT 가 교정 후 폭으로 계산
        self.marker_targets = {}
        self._cover = None
        # 내어쓰기 폭 계산용 글꼴 폭 표: 글꼴 폴더(설정 HANGING_INDENT.font_dir > 환경 변수 LETSHWP_FONT_DIR > <저장소>/fonts)
        hi = config["rules"].get("HANGING_INDENT") or {}
        root = Path(__file__).resolve().parent.parent.parent
        self.font_registry = FontRegistry(hi.get("font_dir") or default_font_dir(root))
        self.missing_fonts = set()

    def rule_cfg(self, rule_id):
        return self.cfg["rules"][rule_id]

    def has_cover(self):
        """별도 표지·목차가 있는 문서인지. 2026-10-28 사용자 결정: 문서에 '목차' 문구가 HY헤드라인M 글꼴로 들어가 있으면 표지 문서.
        반환 (True, 목차 문단 index) 또는 (False, None)."""
        if self._cover is None:
            cfg = self.rule_cfg("FONT_SIZE").get("cover_detection") or {}
            texts = set(cfg.get("toc_heading_texts") or ("목차", "차례"))
            font = cfg.get("font")
            self._cover = (False, None)
            for p in self.paragraphs:
                if p.get("is_empty"):
                    continue
                if re.sub(r"\s+", "", p.get("text") or "") not in texts:
                    continue
                if font is None or (p.get("char_format") or {}).get("font_hangul") == font:
                    self._cover = (True, p["index"])
                    break
        return self._cover

    def text_paragraphs(self):
        """서식 검사 대상 문단(텍스트가 있는 문단)."""
        return [p for p in self.paragraphs if not p.get("is_empty") and p["role"]["role"] not in EXCLUDED_ROLES]

    def visible_char_pr_ids(self, p):
        """문단에서 공백 아닌 글자를 가진 run 의 charPr id 목록(Inspector 의 대표값/혼재 정보 사용)."""
        detail = p.get("char_format_mixed_detail")
        if detail:
            return sorted({cid for d in detail for cid in d["char_pr_ids"]})
        cf = p.get("char_format") or {}
        return [cf["char_pr_id"]] if cf.get("char_pr_id") is not None else []

    def char_pr(self, cid):
        return self.char_props.get(str(cid))

    def font_id(self, lang, face):
        for fid, f in (self.fonts.get(lang) or {}).items():
            if f.get("face") == face:
                return int(fid)
        return None


def _excerpt(s, n=40):
    s = (s or "").replace("\n", " ").replace("\t", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def target_of(p):
    ctx = p["context"]
    t = {
        "type": "paragraph",
        "paragraph_index": p["index"],
        "path": p["path"],
        "container": ctx["container"],
        "text_excerpt": _excerpt(p.get("text")),
    }
    if ctx.get("table_id"):
        t["table_id"] = ctx["table_id"]
        t["cell"] = {"row": ctx["cell"][0], "col": ctx["cell"][1]} if ctx.get("cell") else None
    return t


SPEC_ROLE_LABEL = {"heading_1": "대제목", "heading_2": "중제목"}


def role_of(p):
    r = p["role"]
    if p.get("spec_role"):
        return {"role": p["spec_role"], "label": SPEC_ROLE_LABEL[p["spec_role"]], "confidence": 1.0,
                "source": "항목 번호 기준(1. → 대제목, 가. → 중제목; 분류기 추정 '%s')" % r["label"]}
    out = {"role": r["role"], "label": r["label"], "confidence": r["confidence"], "source": ROLE_SOURCE}
    if r.get("unclassified_reason"):
        out["unclassified_reason"] = r["unclassified_reason"]
    return out


def make_result(doc, rule_id, status, reason, target=None, role=None, current=None, expected=None,
                fix_actions=None, unknown_reason=None, needed_info=None, observation=None, extra=None,
                fix_mode=None, blocked_reason=None, recommendation=None):
    rc = doc.rule_cfg(rule_id)
    mode = fix_mode or FIX_MODE_BY_SPEC.get(rc["spec_mode"], "none")
    fix = {"mode": mode, "possible": False, "recommendation": None, "actions": []}
    if status == FAIL:
        if mode in ("auto", "auto_if_role_confirmed") and fix_actions and not blocked_reason:
            fix["possible"] = True
            fix["actions"] = fix_actions
        if blocked_reason:
            fix["blocked_reason"] = blocked_reason
        fix["recommendation"] = recommendation or _recommendation(rule_id, rc, mode, current, expected, target)
    out = {
        "rule_id": rule_id,
        "rule_name": rc["name"],
        "spec_mode": rc["spec_mode"],
        "spec_text": rc.get("spec_text"),
        "status": status,
        "target": target or {"type": "document"},
        "role": role,
        "current": current,
        "expected": expected,
        "reason": reason,
        "fix": fix,
    }
    if unknown_reason:
        out["unknown_reason"] = unknown_reason
    if needed_info:
        out["needed_info"] = needed_info
    if observation:
        out["observation"] = observation
    if extra:
        out.update(extra)
    return out


def _recommendation(rule_id, rc, mode, current, expected, target):
    cur = (current or {}).get("display")
    exp = (expected or {}).get("display")
    if mode == "manual_review":
        return "자동 수정하지 않음(본문 텍스트 변경이 필요한 항목). '%s' → '%s' 형식으로 작성자가 직접 검토" % (cur, exp)
    if mode == "auto_if_role_confirmed":
        return "추정 역할 '%s'이(가) 맞는지 확인한 뒤 %s 변경: '%s' → '%s'" % (
            (current or {}).get("role_label") or "-", rc["name"], cur, exp)
    return "%s 변경: '%s' → '%s'" % (rc["name"], cur, exp)


def gate_by_role(doc, p, rule_id):
    """역할별 표준값 결정.
    반환: ("check", 표준값) | ("unknown_role", 사유) | ("undecided", 사유) | ("skip", 사유)"""
    rc = doc.rule_cfg(rule_id)
    role = p.get("spec_role") or p["role"]["role"]
    by_role = rc.get("by_role") or {}
    if role in STRUCTURAL_SKIP_ROLES and role not in by_role:
        return "skip", STRUCTURAL_SKIP_ROLES[role]
    if not p.get("spec_role"):
        if role == "unclassified":
            return "unknown_role", "역할 미분류: %s" % (p["role"].get("unclassified_reason") or "근거 부족")
        if p["role"]["confidence"] < doc.role_min:
            return "unknown_role", "역할 신뢰도 %.2f < 기준 %.2f" % (p["role"]["confidence"], doc.role_min)
    if role in by_role and by_role[role] is not None:
        return "check", by_role[role]
    undecided = rc.get("undecided_roles") or {}
    if role in undecided:
        return "undecided", undecided[role]
    return "skip", "명세에 '%s' 역할의 %s 기준이 없음" % (p["role"]["label"], rc["name"])


def skip_group(doc, rule_id, items):
    """SKIP 문단들을 사유별로 묶어 하나의 결과로 만든다(문단마다 SKIP 을 늘어놓지 않기 위함)."""
    out = []
    by_reason = defaultdict(list)
    for p, reason in items:
        by_reason[reason].append(p)
    for reason, ps in by_reason.items():
        out.append(make_result(
            doc, rule_id, SKIP, reason,
            target={"type": "paragraph_group", "paragraph_indices": [p["index"] for p in ps],
                    "count": len(ps)},
            role={"roles": dict(Counter(p["role"]["label"] for p in ps)), "source": ROLE_SOURCE},
            unknown_reason=reason))
    return out


def _role_paragraph_rule(doc, rule_id, current_fn, compare_fn, actions_fn):
    """역할에 따라 표준값이 달라지는 문단 단위 규칙의 공통 흐름."""
    results, skipped = [], []
    for p in doc.text_paragraphs():
        decision, value = gate_by_role(doc, p, rule_id)
        if decision == "skip":
            skipped.append((p, value))
            continue
        current = current_fn(p)
        if current is None:
            results.append(make_result(doc, rule_id, UNKNOWN, "현재값을 추출하지 못함", target_of(p), role_of(p),
                                       unknown_reason="Inspector 결과에 해당 서식값이 없음",
                                       needed_info="해당 문단의 서식 정의(paraPr/charPr)"))
            continue
        current["role_label"] = role_of(p)["label"]
        if decision == "unknown_role":
            results.append(make_result(doc, rule_id, UNKNOWN, "역할이 불확실해 적용할 표준값을 정할 수 없음",
                                       target_of(p), role_of(p), current, unknown_reason=value,
                                       needed_info="문단 역할 확정(사람 확인 또는 분류 규칙 보강)"))
            continue
        if decision == "undecided":
            results.append(make_result(doc, rule_id, UNKNOWN, "해당 역할의 표준값이 명세에서 미확정",
                                       target_of(p), role_of(p), current, unknown_reason=value,
                                       needed_info="FORMAT_SPEC.md 에 해당 역할의 표준값 정의"))
            continue
        res = compare_fn(p, current, value)
        if res is None:
            continue
        status, reason, expected, extra_unknown = res
        if status == UNKNOWN:
            results.append(make_result(doc, rule_id, UNKNOWN, reason, target_of(p), role_of(p), current, expected,
                                       unknown_reason=extra_unknown, needed_info=None))
            continue
        actions = actions_fn(p, current, value) if status == FAIL else None
        results.append(make_result(doc, rule_id, status, reason, target_of(p), role_of(p), current, expected,
                                   fix_actions=actions))
    return results + skip_group(doc, rule_id, skipped)


# ----------------------------------------------------------------------------
# 문서 단위 규칙
# ----------------------------------------------------------------------------
def _page(doc, sec):
    return (sec.get("properties") or {}).get("page")


def rule_paper_size(doc):
    rc = doc.rule_cfg("PAPER_SIZE")
    out = []
    for sec in doc.sections:
        pg = _page(doc, sec)
        target = {"type": "section", "section_index": sec["index"], "path": "sec%d/secPr/pagePr" % sec["index"]}
        expected = {"value": {"width_mm": rc["width_mm"], "height_mm": rc["height_mm"]},
                    "display": "A4 %g×%gmm" % (rc["width_mm"], rc["height_mm"]), "basis": rc["spec_text"]}
        if not pg or pg.get("width_mm") is None or pg.get("height_mm") is None:
            out.append(make_result(doc, "PAPER_SIZE", UNKNOWN, "용지 크기 정보 없음", target, expected=expected,
                                   unknown_reason="secPr/pagePr 미발견", needed_info="구역 설정(hp:pagePr)"))
            continue
        w, h = pg["width_mm"], pg["height_mm"]
        current = {"value": {"width_mm": w, "height_mm": h}, "display": "%s %g×%gmm" % (pg.get("paper") or "비표준", w, h),
                   "source": VALUE_SOURCE, "refs": {"width_hwpunit": pg.get("width_hwpunit"),
                                                    "height_hwpunit": pg.get("height_hwpunit")}}
        tol = rc.get("tolerance_mm", 1.0)
        # 방향은 별도 규칙에서 보므로 짧은 변/긴 변 기준으로 크기만 비교. pass/fail 판정 대상이 아니라 관찰만 보고한다
        same = abs(min(w, h) - rc["width_mm"]) <= tol and abs(max(w, h) - rc["height_mm"]) <= tol
        out.append(make_result(doc, "PAPER_SIZE", UNKNOWN,
                               "용지 크기는 pass/fail 판정 대상이 아니며 강제 수정하지 않음('분석만')",
                               target, current=current, expected=expected,
                               observation="A4(%g×%gmm)와 %s" % (rc["width_mm"], rc["height_mm"], "일치" if same else "다름"),
                               unknown_reason="요구사항 8: 용지 크기는 판정·수정 대상 아님",
                               needed_info=None))
    return out


def rule_paper_orientation(doc):
    rc = doc.rule_cfg("PAPER_ORIENTATION")
    out = []
    for sec in doc.sections:
        pg = _page(doc, sec)
        target = {"type": "section", "section_index": sec["index"], "path": "sec%d/secPr/pagePr" % sec["index"]}
        expected = {"value": rc["orientation"], "display": "세로", "basis": rc["spec_text"]}
        if not pg or not pg.get("orientation_by_dimensions"):
            out.append(make_result(doc, "PAPER_ORIENTATION", UNKNOWN, "용지 방향 정보 없음", target, expected=expected,
                                   unknown_reason="secPr/pagePr 미발견", needed_info="구역 설정(hp:pagePr)"))
            continue
        dims, attr = pg["orientation_by_dimensions"], pg.get("landscape_attr_raw")
        current = {"value": {"by_dimensions": dims, "landscape_attr": attr},
                   "display": "치수 기준 %s, landscape=%s" % ("세로" if dims == "PORTRAIT" else "가로" if dims == "LANDSCAPE" else dims, attr),
                   "source": VALUE_SOURCE}
        if dims == rc["orientation"] and attr in rc["landscape_attr_observed_portrait"]:
            out.append(make_result(doc, "PAPER_ORIENTATION", PASS,
                                   "세로 치수(폭<높이)이고 landscape=%s 는 세로 문서에서 관측된 조합과 일치" % attr,
                                   target, current=current, expected=expected))
        else:
            out.append(make_result(doc, "PAPER_ORIENTATION", UNKNOWN,
                                   "landscape 속성 의미가 공식 근거로 확인되지 않아 최종 방향을 확정할 수 없음",
                                   target, current=current, expected=expected,
                                   unknown_reason="landscape=%s 와 치수(%s) 조합은 관측 사례에 없음" % (attr, dims),
                                   needed_info="한글에서 같은 문서를 가로/세로로 저장해 landscape 속성 의미 확인"))
    return out


def _margin_actions(sec, bad, std, xml_sides):
    return [{"op": "set_page_margin", "section_index": sec["index"],
             "xml": "Contents/section%d.xml hp:secPr/hp:pagePr/hp:margin@%s" % (sec["index"], ",".join(xml_sides)),
             "from_mm": bad, "to_mm": {k: std[k] for k in bad},
             "note": "mm → HWPUNIT(1mm = 7200/25.4, 소수점 이하 버림: 한글 저장값과 일치). 본문 영역이 바뀌어 줄·쪽 나눔이 달라질 수 있음"}]


def rule_page_margin(doc):
    rc = doc.rule_cfg("PAGE_MARGIN")
    std = rc["values_mm"]
    sides = ("top", "bottom", "left", "right")
    out = []
    for sec in doc.sections:
        pg = _page(doc, sec) or {}
        m = pg.get("margin_mm")
        target = {"type": "section", "section_index": sec["index"], "path": "sec%d/secPr/pagePr/margin" % sec["index"]}
        expected = {"value": std, "display": "위 %g / 아래 %g / 왼쪽 %g / 오른쪽 %gmm" % tuple(std[k] for k in sides),
                    "basis": rc["spec_text"]}
        if not m or any(m.get(k) is None for k in sides):
            out.append(make_result(doc, "PAGE_MARGIN", UNKNOWN, "여백 정보 없음", target, expected=expected,
                                   unknown_reason="hp:pagePr/hp:margin 미발견", needed_info="구역 설정"))
            continue
        current = {"value": {k: m[k] for k in sides},
                   "display": "위 %g / 아래 %g / 왼쪽 %g / 오른쪽 %gmm" % tuple(m[k] for k in sides), "source": VALUE_SOURCE}
        tol = doc.cfg.get("tolerance_mm", 0.5)
        bad = {k: m[k] for k in sides if abs(m[k] - std[k]) > tol}
        if not bad:
            out.append(make_result(doc, "PAGE_MARGIN", PASS, "쪽 여백이 표준값과 일치(허용 오차 ±%gmm)" % tol,
                                   target, current=current, expected=expected))
        else:
            label = {"top": "위", "bottom": "아래", "left": "왼쪽", "right": "오른쪽"}
            out.append(make_result(doc, "PAGE_MARGIN", FAIL,
                                   "쪽 여백 %s ≠ 표준" % ", ".join("%s %gmm(표준 %gmm)" % (label[k], v, std[k]) for k, v in bad.items()),
                                   target, current=current, expected=expected,
                                   fix_actions=_margin_actions(sec, bad, std, list(bad))))
    return out


def rule_header_footer_margin(doc):
    rc = doc.rule_cfg("HEADER_FOOTER_MARGIN")
    std = {"header": rc["header_mm"], "footer": rc["footer_mm"]}
    out = []
    for sec in doc.sections:
        m = (_page(doc, sec) or {}).get("margin_mm")
        target = {"type": "section", "section_index": sec["index"], "path": "sec%d/secPr/pagePr/margin" % sec["index"]}
        expected = {"value": std, "display": "머리말 %g / 꼬리말 %gmm" % (std["header"], std["footer"]), "basis": rc["spec_text"]}
        if not m or m.get("header") is None or m.get("footer") is None:
            out.append(make_result(doc, "HEADER_FOOTER_MARGIN", UNKNOWN, "머리말·꼬리말 여백 정보 없음", target,
                                   expected=expected, unknown_reason="hp:margin@header/@footer 미발견",
                                   needed_info="구역 설정"))
            continue
        current = {"value": {"header": m["header"], "footer": m["footer"]},
                   "display": "머리말 %g / 꼬리말 %gmm" % (m["header"], m["footer"]), "source": VALUE_SOURCE}
        tol = doc.cfg.get("tolerance_mm", 0.5)
        bad = {k: m[k] for k in ("header", "footer") if abs(m[k] - std[k]) > tol}
        if not bad:
            out.append(make_result(doc, "HEADER_FOOTER_MARGIN", PASS, "머리말·꼬리말 여백이 표준값과 일치",
                                   target, current=current, expected=expected))
        else:
            label = {"header": "머리말", "footer": "꼬리말"}
            out.append(make_result(doc, "HEADER_FOOTER_MARGIN", FAIL,
                                   "%s ≠ 표준" % ", ".join("%s %gmm(표준 %gmm)" % (label[k], v, std[k]) for k, v in bad.items()),
                                   target, current=current, expected=expected,
                                   fix_actions=_margin_actions(sec, bad, std, list(bad))))
    return out


def rule_page_number(doc):
    """쪽 번호(2026-10-31 사용자 결정): 아래 가운데 '- 1 -' 형식. 없으면 추가, 다른 양식이 있으면 바꾸지 않는다."""
    rc = doc.rule_cfg("PAGE_NUMBER")
    want = {"pos": rc["pos"], "format": rc["format"], "side_char": rc["side_char"]}
    out = []
    for sec in doc.sections:
        target = {"type": "section", "section_index": sec["index"], "path": "sec%d/pageNum" % sec["index"]}
        expected = {"value": want, "display": "아래 가운데, 숫자, 양옆 '%s' ('%s 1 %s')" % (rc["side_char"], rc["side_char"], rc["side_char"]),
                    "basis": rc["spec_text"]}
        pns = sec.get("page_numbers") or []
        if not pns:
            first = next((p for p in doc.paragraphs if p.get("section") == sec["index"] and p["path"] == "sec%d/p0" % sec["index"]), None)
            out.append(make_result(doc, "PAGE_NUMBER", FAIL, "쪽 번호 컨트롤(hp:pageNum)이 없음", target,
                                   current={"value": None, "display": "쪽 번호 없음", "source": VALUE_SOURCE + " - hp:ctrl/hp:pageNum"},
                                   expected=expected,
                                   fix_actions=[{"op": "add_page_number", "section_index": sec["index"], "to": want,
                                                 "paragraph_index": first["index"] if first else None,
                                                 "xml": "Contents/section%d.xml 첫 문단의 구역 설정 run 끝에 <hp:ctrl><hp:pageNum/></hp:ctrl> 추가" % sec["index"],
                                                 "note": "구조 불변 원칙의 예외: 쪽 번호 컨트롤 하나만 추가(글자·문단은 그대로)"}]))
            continue
        pn = pns[0]
        current = {"value": pn, "display": "%s, %s, 양옆 '%s'" % (pn.get("pos"), pn.get("format"), pn.get("side_char")),
                   "source": VALUE_SOURCE + " - hp:ctrl/hp:pageNum"}
        if all(pn.get(k) == v for k, v in want.items()):
            out.append(make_result(doc, "PAGE_NUMBER", PASS, "쪽 번호가 표준 양식과 일치", target, current=current, expected=expected))
        else:
            out.append(make_result(doc, "PAGE_NUMBER", UNKNOWN, "다른 양식의 쪽 번호가 이미 있음 - 기존 컨트롤은 바꾸지 않음", target,
                                   current=current, expected=expected, unknown_reason="기존 쪽 번호 양식 변경은 범위 밖(추가만 지원)",
                                   needed_info="기존 쪽 번호를 바꿀지 결정"))
    return out


def rule_doc_title_box(doc):
    rc = doc.rule_cfg("DOC_TITLE_BOX")
    out = []
    boxes = _title_boxes(doc)
    has_toc = any(p["role"]["role"] == "toc" for p in doc.paragraphs)
    if not boxes:
        out.append(make_result(doc, "DOC_TITLE_BOX", UNKNOWN, "제목 박스(문서 제목을 담은 1열 표)를 찾지 못함",
                               expected={"display": "표지·목차가 없으면 상단 제목 박스 사용", "basis": rc["spec_text"]},
                               unknown_reason="표지·목차 존재 여부 판단 기준이 미확정(FORMAT_SPEC 5)이라 제목 박스 필요 여부를 판단할 수 없음"
                                              + (" (차례 문단 감지됨)" if has_toc else ""),
                               needed_info="표지·목차 판단 기준"))
        return out
    for t, titles in boxes:
        p = doc.paragraphs[titles[0]]
        target = {"type": "table", "table_id": t["id"], "path": t["path"], "paragraph_index": p["index"],
                  "text_excerpt": _excerpt(p["text"])}
        h = (t.get("size") or {}).get("height_mm")
        expected = {"value": rc["height_mm"], "display": "높이 %gmm" % rc["height_mm"], "basis": rc["spec_text"]}
        if h is None:
            out.append(make_result(doc, "DOC_TITLE_BOX", UNKNOWN, "제목 박스 높이 정보 없음", target, role_of(p),
                                   expected=expected, unknown_reason="hp:sz@height 없음", needed_info="표 크기"))
        else:
            tol = doc.cfg.get("tolerance_mm", 0.5)
            ok = abs(h - rc["height_mm"]) <= tol
            current = {"value": h, "display": "높이 %gmm" % h, "source": VALUE_SOURCE, "refs": {"table_id": t["id"]},
                       "role_label": p["role"]["label"]}
            out.append(make_result(doc, "DOC_TITLE_BOX", PASS if ok else FAIL,
                                   "제목 박스 표 높이 %gmm (허용오차 ±%gmm)" % (h, tol),
                                   target, role_of(p), current, expected,
                                   extra={"sub_item": "높이"}))
        # 양단선: 폭(1mm)은 명시되었으나 '양단선'이 어느 테두리를 뜻하는지는 명시되지 않음
        cells = t["cells"]
        borders = (cells[0].get("borders") or {}) if len(cells) == 1 else {}
        obs = {side: (borders.get(side) or {}).get("width") for side in ("top", "bottom", "left", "right")}
        current = {"value": obs, "display": "위 %s / 아래 %s / 왼쪽 %s / 오른쪽 %s" % tuple(obs[s] for s in ("top", "bottom", "left", "right")),
                   "source": VALUE_SOURCE + " (셀 테두리 borderFill)"}
        tb_match = all(_mm(obs[s]) is not None and abs(_mm(obs[s]) - rc["double_line_width_mm"]) <= 0.05 for s in ("top", "bottom"))
        out.append(make_result(doc, "DOC_TITLE_BOX", UNKNOWN, "'양단선'이 가리키는 테두리(위·아래 등)가 명세에 정의되지 않아 판정하지 않음",
                               target, role_of(p), current,
                               {"value": rc["double_line_width_mm"], "display": "양단선 %gmm" % rc["double_line_width_mm"], "basis": rc["spec_text"]},
                               unknown_reason="'양단선' 정의 미확정",
                               needed_info="양단선의 의미(위·아래 테두리 여부, 선 종류) 확정",
                               observation="위·아래 테두리로 해석하면 %s" % ("일치" if tb_match else "불일치"),
                               extra={"sub_item": "양단선"}))
    return out


def _mm(width_str):
    if not width_str:
        return None
    m = re.match(r"\s*([\d.]+)\s*mm", width_str)
    return float(m.group(1)) if m else None


def _is_hangul_syllable(ch):
    return 0xAC00 <= ord(ch) <= 0xD7A3


def _midword_line_breaks(doc, p):
    """한글이 저장한 줄 배치 캐시(linesegarray)에서 한글 어절 중간(음절과 음절 사이)에서 줄이 시작하는 횟수.
    캐시를 신뢰할 수 없거나(작성 프로그램이 한글이 아님) 글자 위치를 텍스트에 대응시킬 수 없으면 None."""
    ll = p.get("line_layout")
    text = p.get("text") or ""
    trusted, _ = _layout_trusted(doc)
    if not ll or not trusted or ll.get("line_count", 0) < 2 or p.get("objects") or p.get("controls") \
            or "\t" in text or "\n" in text:
        return None
    n = 0
    for ln in ll["lines"][1:]:
        tp = ln.get("textpos")
        if tp is None or tp <= 0 or tp >= len(text):
            return None
        if _is_hangul_syllable(text[tp - 1]) and _is_hangul_syllable(text[tp]):
            n += 1
    return n


def rule_word_break(doc):
    """어절 단위 줄 나눔(요구사항 1 예외): 문단 모양 hh:breakSetting 의 줄 나눔 기준이 한글 단위 '어절', 영어 단위 '단어'인지.
    한글 저장 HWPX 는 breakNonLatinWord 의 값 이름이 동작과 반대(BREAK_WORD=어절, KEEP_WORD=글자)다. 근거는 설정 _comment 와
    docs/HWPX_STRUCTURE.md 참조. 줄바꿈 위치와 양쪽 정렬의 공백 조정은 한글이 문서를 열 때 다시 계산한다."""
    rc = doc.rule_cfg("WORD_BREAK")
    if rc.get("enabled") is False:
        # 규칙을 끈 경우(2026-10-31 사용자 결정): 검사·수정하지 않고 원본 줄 나눔 기준을 유지
        return skip_group(doc, "WORD_BREAK", [(p, rc.get("disabled_reason") or "규칙 꺼짐(설정 enabled=false)")
                                              for p in doc.text_paragraphs()])
    attrs =((rc["hangul_attr"], rc["hangul_word_unit_value"], "한글 단위", rc.get("hangul_value_labels") or {}),
             (rc["latin_attr"], rc["latin_word_unit_value"], "영어 단위", rc.get("latin_value_labels") or {}))
    want = {a: v for a, v, _, _ in attrs}

    def display(values):
        return " / ".join("%s %s" % (label, labels.get(values.get(a), values.get(a))) for a, _, label, labels in attrs)

    expected = {"value": want, "display": display(want), "basis": rc["spec_text"]}
    results = []
    for p in doc.text_paragraphs():
        pf = p.get("para_format") or {}
        pid = pf.get("para_pr_id")
        brk = ((doc.para_props.get(str(pid)) if pid is not None else None) or {}).get("break")
        if not brk or any(brk.get(a) is None for a in want):
            results.append(make_result(doc, "WORD_BREAK", UNKNOWN, "문단 모양의 줄 나눔 기준(hh:breakSetting)을 추출하지 못함",
                                       target_of(p), role_of(p), expected=expected,
                                       unknown_reason="paraPr %s 의 breakSetting@%s 없음" % (pid, "/".join(want)),
                                       needed_info="문단 모양 정의"))
            continue
        current = {"value": {a: brk[a] for a in want}, "display": display(brk),
                   "source": VALUE_SOURCE + " - paraPr/breakSetting@%s" % ",".join(want), "refs": {"para_pr_id": pid},
                   "role_label": p["role"]["label"]}
        mid = _midword_line_breaks(doc, p)
        obs = ("한글이 저장한 줄 배치에서 어절 중간에 줄이 나뉜 곳 %d개(수정본은 한글이 열 때 다시 배치)" % mid) if mid else None
        bad = {a: brk[a] for a in want if brk[a] != want[a]}
        if not bad:
            results.append(make_result(doc, "WORD_BREAK", PASS, "줄 나눔 기준이 표준(%s)과 일치" % expected["display"],
                                       target_of(p), role_of(p), current, expected, observation=obs))
            continue
        actions = [{"op": "set_para_word_break", "para_pr_id": pid,
                    "xml": "Contents/header.xml hh:paraPr[@id=%s]/hh:breakSetting@%s" % (pid, ",".join(sorted(bad))),
                    "from": bad, "to": {a: want[a] for a in bad},
                    "note": "한글 저장 HWPX 의 breakNonLatinWord 는 값 이름이 동작과 반대(BREAK_WORD=어절, KEEP_WORD=글자). "
                            "줄 나눔 위치가 바뀌므로 한글에서 열어 화면 배치 확인"}]
        results.append(make_result(doc, "WORD_BREAK", FAIL, "줄 나눔 기준 %s ≠ 표준 %s" % (current["display"], expected["display"]),
                                   target_of(p), role_of(p), current, expected, fix_actions=actions, observation=obs))
    return results


TOC_HEADING_TEXT = re.compile(r"^\s*(목\s*차|차\s*례|contents|table of contents)\s*$", re.I)


def _sizes_of(doc, p):
    """문단의 보이는 run 별 글자 크기(pt): {charPr id: size}"""
    out = {}
    for cid in doc.visible_char_pr_ids(p):
        cp = doc.char_pr(cid)
        if cp and cp.get("size_pt") is not None:
            out[cid] = cp["size_pt"]
    return out


def _body_size_decision(doc, p, by_role):
    """'본문' 역할 문단의 글자 크기 기준. 2026-10-28 사용자 결정: 일반 본문(번호·기호 없는 본문 최상위 문단)은 14pt.
    반환 ("check", pt, 근거) | ("unknown", 사유, 필요 정보) | ("skip", 사유)"""
    h = p.get("hierarchy") or {}
    kind = h.get("kind")
    if kind == "body":
        if p["role"]["confidence"] < doc.role_min:
            return "unknown", "역할 신뢰도 %.2f < 기준 %.2f" % (p["role"]["confidence"], doc.role_min), "문단 역할 확정"
        return "check", by_role["body"], "일반 본문"
    if kind == "item":
        return "skip", "번호 항목(%s)의 글자 크기 표준은 요구사항에 없음(1)·가)·①·㉮ 표준 미정)" % h.get("marker")
    if kind == "unmapped_item":
        return "skip", "표준 체계에 없는 번호·기호('%s') 항목의 글자 크기 기준 없음" % h.get("marker")
    if kind == "bullet":
        return "skip", "'%s' 기호 항목의 글자 크기 표준은 요구사항에 없음" % h.get("marker")
    if kind == "out_of_scope":
        return "skip", "본문 영역 밖(글상자 등)의 본문 문단 - 일반 본문 14pt 기준은 본문 최상위 문단에만 적용"
    return "skip", "번호·기호 없는 본문 문단이 아니어서 일반 본문 기준을 적용할 수 없음"


def rule_font_size(doc):
    """역할별 글자 크기(요구사항 3·4·5). 주석은 바로 위(주석 아닌) 문단보다 2pt 작게(그 문단이 수정될 예정이면 수정 후 크기 기준),
    항목 기호 □·ㅇ·-·․ 와 일반 본문 14pt(대제목 바로 다음 □ 는 15pt 허용), 문서 제목은 표지 유무('목차' HY헤드라인M)로 22/24pt."""
    rc = doc.rule_cfg("FONT_SIZE")
    tol = rc.get("tolerance_pt", 0.05)
    by_role = rc.get("by_role") or {}
    undecided = rc.get("undecided_roles") or {}
    note_markers = tuple(doc.rule_cfg("PARA_SPACING").get("note_markers") or ("*", "※"))
    bullet_pt = rc.get("bullet_pt") or {}
    bullet_after_h1 = rc.get("bullet_after_heading_1_allowed_pt") or {}
    title_pt = rc.get("document_title_pt") or {}
    results, skipped = [], []
    last_size = {}   # (영역, 표) -> 직전(주석 아닌) 문단의 대표 글자 크기(이 규칙이 바꿀 예정이면 목표값)
    for p in doc.paragraphs:
        if p.get("is_empty") or p["role"]["role"] in EXCLUDED_ROLES:
            continue
        role = p.get("spec_role") or p["role"]["role"]
        ctx = p["context"]
        key = (ctx.get("container"), ctx.get("table_id"), ctx.get("object_id"))
        text = p.get("text") or ""
        h = p.get("hierarchy") or {}
        sizes = _sizes_of(doc, p)
        dominant = (p.get("char_format") or {}).get("size_pt")
        is_note = note_marker_of(text, note_markers) is not None
        current = None
        if sizes:
            distinct = sorted(set(sizes.values()))
            current = {"value": distinct[0] if len(distinct) == 1 else distinct,
                       "display": ", ".join("%gpt" % v for v in distinct) + (" (혼재)" if len(distinct) > 1 else ""),
                       "source": VALUE_SOURCE + " - charPr@height", "refs": {"char_pr_sizes": sizes},
                       "role_label": role_of(p)["label"]}
        expected_pt, basis, allowed, unknown = None, None, (), None
        if role == "toc":
            heading = bool(TOC_HEADING_TEXT.match(text))
            expected_pt = rc["toc_heading_pt"] if heading else rc["toc_entry_pt"]
            basis = "목차 %s" % ("제목" if heading else "항목(하위 제목·붙임·별첨)")
        elif is_note or role == "note":
            prev = last_size.get(key)
            ps = h.get("previous_paragraph")
            prev_p = doc.paragraphs[ps] if ps is not None else None
            under_table = bool(prev_p and (prev_p.get("hierarchy") or {}).get("kind") == "object"
                               and any(str(o).startswith("T") for o in prev_p.get("objects") or []))
            if under_table:
                # 표 바로 아래 주석은 '바로 위 문장 -2pt' 규칙에서 제외(2026-10-28 사용자 결정)
                skipped.append((p, "표 바로 아래의 참고·주석은 글자 크기 규칙(바로 위 문장 -2pt)에서 제외"))
                continue
            if not is_note:
                unknown = ("'*'·'※' 로 시작하지 않는 참고·주석 문단의 글자 크기 기준은 요구사항에 없음", "주석 기호 없는 참고 문단의 기준")
            elif prev is None:
                unknown = ("바로 위 문장의 글자 크기를 알 수 없음(같은 영역의 앞 문단 없음)", "바로 위 문장의 글자 크기")
            else:
                expected_pt = prev + rc["note_relative_pt"]
                basis = "바로 위 문장 %gpt %+dpt" % (prev, rc["note_relative_pt"])
        elif h.get("kind") == "bullet" and h.get("marker") in bullet_pt:
            expected_pt = bullet_pt[h["marker"]]
            basis = "%s 항목" % h["marker"]
            ps = h.get("previous_structural_paragraph")
            prev_h = (doc.paragraphs[ps].get("hierarchy") or {}) if ps is not None else {}
            if prev_h.get("kind") == "item" and prev_h.get("level") == 1 and h["marker"] in bullet_after_h1:
                allowed = (bullet_after_h1[h["marker"]],)
                basis += " (대제목 #%d 바로 다음이라 %gpt 허용)" % (ps, allowed[0])
        elif role == "unclassified":
            unknown = ("역할 미분류: %s" % (p["role"].get("unclassified_reason") or "근거 부족"), "문단 역할 확정")
        elif role == "document_title" and title_pt:
            if p["role"]["confidence"] < doc.role_min:
                unknown = ("역할 신뢰도 %.2f < 기준 %.2f" % (p["role"]["confidence"], doc.role_min), "문단 역할 확정")
            else:
                cover, toc_idx = doc.has_cover()
                expected_pt = title_pt.get("with_cover" if cover else "without_cover")
                basis = ("표지 제목 (문단 #%d '%s' 가 %s 글꼴 → 별도 표지·목차 문서)" % (
                            toc_idx, _excerpt(doc.paragraphs[toc_idx]["text"]), (rc.get("cover_detection") or {}).get("font"))
                         if cover else "본문 상단 문서 제목 (%s 글꼴의 '목차' 문단 없음 → 표지 없는 문서)" % (rc.get("cover_detection") or {}).get("font"))
                if expected_pt is None:
                    unknown = ("문서 제목 글자 크기 표준이 설정에 없음", "FORMAT_SPEC.md 의 문서 제목 글자 크기")
        elif role == "body" and "body" in by_role:
            decision = _body_size_decision(doc, p, by_role)
            if decision[0] == "check":
                expected_pt, basis = decision[1], decision[2]
            elif decision[0] == "unknown":
                unknown = (decision[1], decision[2])
            else:
                skipped.append((p, decision[1]))
                if dominant:
                    last_size[key] = dominant
                continue
        elif role in by_role:
            if not p.get("spec_role"):
                # 번호 없이 제목으로 '추정'된 문단은 크기를 바꾸면 추정 역할이 바뀔 수 있어 기준을 적용하지 않는다
                unknown = ("번호 없이 %s(으)로 추정된 문단 - 글자 크기 기준은 '1.'/'가.' 로 시작하는 대제목·중제목에만 적용" % p["role"]["label"],
                           "문단이 대제목/중제목인지 확인(번호 표기)")
            else:
                expected_pt, basis = by_role[role], role_of(p)["label"]
        elif role in undecided:
            unknown = (undecided[role], "FORMAT_SPEC.md 에 해당 역할의 표준값 정의")
        else:
            skipped.append((p, "'%s' 역할의 글자 크기 표준은 요구사항에 없음(표준 미정)" % p["role"]["label"]))
            if dominant:
                last_size[key] = dominant
            continue
        if unknown:
            if not (is_note or role == "note") and dominant:
                last_size[key] = dominant
            results.append(make_result(doc, "FONT_SIZE", UNKNOWN, "적용할 글자 크기 기준을 정할 수 없음", target_of(p), role_of(p),
                                       current, unknown_reason=unknown[0], needed_info=unknown[1]))
            continue
        if current is None:
            results.append(make_result(doc, "FONT_SIZE", UNKNOWN, "현재값을 추출하지 못함", target_of(p), role_of(p),
                                       unknown_reason="charPr 정의 없음", needed_info="글자 모양 정의"))
            continue
        expected = {"value": expected_pt, "display": "%gpt" % expected_pt + (" (또는 %gpt)" % allowed[0] if allowed else ""),
                    "basis": basis}
        bad = {cid: v for cid, v in sizes.items()
               if abs(v - expected_pt) > tol and not any(abs(v - a) <= tol for a in allowed)}
        if not (is_note or role == "note") and dominant:
            # 다음 주석의 기준은 '바로 위 문장'의 수정 후 크기: 이 문단이 수정 대상이면 목표값, 아니면 현재 대표 크기
            last_size[key] = expected_pt if bad else dominant
        if bad:
            doc.font_size_targets[p["index"]] = {cid: expected_pt for cid in bad}
            actions = [{"op": "set_char_size", "char_pr_id": cid,
                        "xml": "Contents/header.xml hh:charPr[@id=%d]@height" % cid, "from": v, "to": expected_pt}
                       for cid, v in bad.items()]
            results.append(make_result(doc, "FONT_SIZE", FAIL, "글자 크기 %s ≠ 기준 %s (%s)" % (current["display"], expected["display"], basis),
                                       target_of(p), role_of(p), current, expected, fix_actions=actions))
        else:
            results.append(make_result(doc, "FONT_SIZE", PASS, "글자 크기가 기준(%s)과 일치 (%s)" % (expected["display"], basis),
                                       target_of(p), role_of(p), current, expected))
    return results + skip_group(doc, "FONT_SIZE", skipped)


def _title_boxes(doc):
    """본문 최상위의 1열·3행 이하 표 중 '문서 제목' 역할 문단을 담은 표(제목 박스)."""
    boxes = []
    for t in doc.tables.values():
        if t.get("context") != "body" or t.get("nesting_depth", 0) != 0 or t.get("cols") != 1 or (t.get("rows") or 0) > 3:
            continue
        titles = [i for c in t["cells"] for i in c["paragraphs"]
                  if doc.paragraphs[i]["role"]["role"] == "document_title"]
        if titles:
            boxes.append((t, titles))
    return boxes


def _body_scope(doc):
    return [p for p in doc.paragraphs if (p.get("hierarchy") or {}).get("kind") not in (None, "out_of_scope")]


def _type_special(doc):
    """TYPE A/B 특례(5.6) 적용 대상과 확인 결과.
    반환 None(대상 없음) 또는 {"paragraph", "status": applied|unconfirmed, "expected", "reason", "needed_info"}"""
    rc = doc.rule_cfg("PARA_SPACING")
    scope = _body_scope(doc)
    first = next((p for p in scope if p["hierarchy"]["kind"] == "item" and p["hierarchy"]["level"] == 1), None)
    if first is None:
        return None
    boxes = [t for t, _ in _title_boxes(doc) if t["anchor_paragraph"] < first["index"]]
    tocs = [p["index"] for p in doc.paragraphs if p["role"]["role"] == "toc" and p["index"] < first["index"]]
    dt = (doc.doc_type or "").upper() or None
    out = {"paragraph": first["index"], "doc_type": dt}
    if dt is None:
        if boxes or tocs:
            hint = "제목 표" if boxes else "목차"
            return dict(out, status="unconfirmed", expected=None,
                        reason="문서 첫 1단계 항목 앞에 %s가 있어 TYPE %s 특례 대상일 수 있으나 문서 유형이 지정되지 않음"
                               % (hint, "A" if boxes else "B"),
                        needed_info="TYPE A/B 판별 기준 확정(FORMAT_SPEC 6) 또는 --doc-type A|B 지정")
        return None
    between = lambda a, b: [p for p in scope if a < p["index"] < b]
    if dt == "A":
        if not boxes:
            return dict(out, status="unconfirmed", expected=None,
                        reason="TYPE A 지정이나 첫 1단계 항목 앞에서 제목 표(문서 제목을 담은 1열 표)를 찾지 못함",
                        needed_info="제목 표 위치 확인")
        anchor = boxes[0]["anchor_paragraph"]
        before = [p for p in scope if p["index"] < anchor and p["hierarchy"]["kind"] != "empty"]
        mid = between(anchor, first["index"])
        if before:
            return dict(out, status="unconfirmed", expected=None,
                        reason="제목 표 앞에 다른 내용(#%d)이 있어 첫 페이지 제목 표인지 확인할 수 없음" % before[0]["index"],
                        needed_info="첫 페이지 구성 확인")
        if any(p["hierarchy"]["kind"] != "empty" for p in mid):
            return dict(out, status="unconfirmed", expected=None,
                        reason="제목 표와 첫 1단계 항목 사이에 다른 내용이 있어 두 요소 사이 간격으로 볼 수 없음",
                        needed_info="대상 문단 확인")
        if any(p.get("page_break_before") for p in mid + [first]):
            return dict(out, status="unconfirmed", expected=None,
                        reason="제목 표와 첫 1단계 항목 사이에 쪽 나눔이 있어 같은 첫 페이지인지 확인할 수 없음",
                        needed_info="첫 페이지 구성 확인")
        return dict(out, status="applied", expected=rc["type_a_first_item_pt"],
                    reason="TYPE A: 첫 페이지 제목 표(%s) → 첫 1단계 항목" % boxes[0]["id"],
                    blank_between=len(mid))
    if dt == "B":
        if not tocs:
            return dict(out, status="unconfirmed", expected=None,
                        reason="TYPE B 지정이나 첫 1단계 항목 앞에서 목차(차례) 문단을 찾지 못함",
                        needed_info="목차 위치 확인")
        if not first.get("page_break_before"):
            return dict(out, status="unconfirmed", expected=None,
                        reason="첫 1단계 항목에 쪽 나눔(pageBreak) 속성이 없어 목차 다음 페이지 시작인지 확인할 수 없음",
                        needed_info="쪽 시작 위치 확인(렌더링 또는 쪽 나눔 속성)")
        return dict(out, status="applied", expected=rc["type_b_first_item_pt"],
                    reason="TYPE B: 목차(#%d) 다음 쪽에서 시작하는 첫 1단계 항목" % tocs[-1])
    return dict(out, status="unconfirmed", expected=None, reason="알 수 없는 문서 유형 '%s'" % dt,
                needed_info="문서 유형은 A 또는 B")


def _item_label(level, marker):
    return "%d단계 '%s'" % (level, marker or LEVEL_EXAMPLE.get(level, "?"))


def rule_para_spacing(doc):
    """문단 위 간격(요구사항 7): 항목 번호는 자기 단계 기준(1. 20 / 가. 16 / 그 외 14),
    항목 기호는 직전 항목과의 층위 관계(□ 사이 14, 한 층위 아래 6), 참고·주석 6, 일반 본문 6."""
    rc = doc.rule_cfg("PARA_SPACING")
    tol = rc.get("tolerance_pt", 0.05)
    special = _type_special(doc)
    results, skipped = [], []
    for p in doc.paragraphs:
        h = p.get("hierarchy") or {}
        kind = h.get("kind")
        if kind in (None, "empty"):
            continue
        if kind == "out_of_scope":
            if not p.get("is_empty") and p["role"]["role"] not in EXCLUDED_ROLES:
                skipped.append((p, "본문 최상위 문단이 아님(표·머리말·각주·글상자 등) - 문단 간격 규칙 대상 아님"))
            continue
        if kind == "object":
            skipped.append((p, "텍스트 없이 표·그림만 있는 문단 - 문단 간격 규칙 대상 아님"))
            continue
        if kind == "other":
            skipped.append((p, "번호·기호·주석 기호 없는 %s 문단 - 문단 간격 규칙(항목/주석/일반 본문)으로 간격을 정할 수 없음" % p["role"]["label"]))
            continue
        if kind == "note" and rc.get("note_above_pt") is None:
            skipped.append((p, rc.get("note_above_excluded_reason") or "참고·주석 위 간격 기준 없음(검사 제외)"))
            continue

        pf = p.get("para_format") or {}
        cur_v = pf.get("space_before_pt")
        current = None if cur_v is None else {
            "value": cur_v, "display": "문단 위 %gpt" % cur_v, "source": VALUE_SOURCE + " - paraPr/margin@prev",
            "refs": {"para_pr_id": pf.get("para_pr_id")}, "role_label": p["role"]["label"]}
        hier = {k: h.get(k) for k in ("kind", "level", "marker", "prev_marked", "owner_item",
                                      "previous_paragraph", "previous_structural_paragraph", "blank_paragraphs_before")}
        observations = []
        if h.get("blank_paragraphs_before"):
            observations.append("앞에 빈 문단 %d개 - 빈 줄이 간격 역할(빈 문단은 삭제하지 않음)" % h["blank_paragraphs_before"])
        prev = doc.paragraphs[h["previous_paragraph"]] if h.get("previous_paragraph") is not None else None
        prev_after = ((prev or {}).get("para_format") or {}).get("space_after_pt")
        if prev_after:
            observations.append("직전 문단 #%d 의 문단 아래 간격 %gpt 가 별도로 있음" % (prev["index"], prev_after))

        expected_pt, basis, allowed = None, None, ()
        blanks = h.get("blank_paragraphs_before") or 0
        if kind == "item" and h.get("level") == 1 and p.get("page_break_before"):
            # 쪽 나눔 바로 뒤의 대제목은 새 쪽 첫 줄이므로 문단 위 간격 0pt (2026-10-31 사용자 결정)
            expected_pt, basis = 0, "쪽 나눔 바로 뒤의 대제목 → 문단 위 간격 0pt"
        elif blanks and kind in ("item", "bullet", "note", "body") and not (special and special["paragraph"] == p["index"]):
            # 빈 문단(빈 줄)으로 간격을 둔 문단: 빈 줄이 간격 역할을 하므로 문단 위 간격은 0pt (sample_original 기준, 2026-10-31)
            expected_pt = 0
            basis = "앞에 빈 문단 %d개가 간격 역할 → 문단 위 간격 0pt (빈 문단은 삭제하지 않음)" % blanks
        elif special and special["paragraph"] == p["index"]:
            if special["status"] != "applied":
                results.append(make_result(doc, "PARA_SPACING", UNKNOWN, "TYPE A/B 특례 대상 여부를 확정할 수 없음",
                                           target_of(p), role_of(p), current, unknown_reason=special["reason"],
                                           needed_info=special["needed_info"],
                                           observation="; ".join(observations) or None,
                                           extra={"hierarchy": hier, "sub_item": "TYPE A/B 특례"}))
                continue
            expected_pt, basis = special["expected"], special["reason"]
        elif kind == "unmapped_item":
            results.append(make_result(doc, "PARA_SPACING", UNKNOWN, "항목의 번호·기호가 표준 체계에 없어 간격 기준을 정할 수 없음",
                                       target_of(p), role_of(p), current,
                                       unknown_reason="번호·기호 '%s' 는 표준 번호(1. 가. 1) 가) ① ㉮)·기호(□ ㅇ - ․) 체계에 없음" % h.get("marker"),
                                       needed_info="표준 번호·기호로 정리(항목 번호·항목 기호 규칙 참조) 또는 해당 표기의 간격 기준 추가",
                                       extra={"hierarchy": hier}))
            continue
        elif kind == "item":
            expected_pt, why = item_above_spacing(h["level"], rc)
            basis = "%s 항목 위 (%s)" % (_item_label(h["level"], h.get("marker")), why)
        elif kind == "bullet":
            ps = h.get("previous_structural_paragraph")
            prev_h = (doc.paragraphs[ps].get("hierarchy") or {}) if ps is not None else {}
            after_h1 = prev_h.get("kind") == "item" and prev_h.get("level") == 1
            pt, allowed, why = bullet_above_spacing(h.get("prev_marked"), h["level"], rc, after_h1)
            if pt is None:
                if h.get("prev_marked") is None and h.get("prev_marked_unavailable_reason"):
                    why = h["prev_marked_unavailable_reason"]
                results.append(make_result(doc, "PARA_SPACING", UNKNOWN, "기호 항목 '%s' 위 간격의 기준이 없음" % h.get("marker"),
                                           target_of(p), role_of(p), current, unknown_reason=why,
                                           needed_info="요구사항 7(항목 기호 사이 간격)에 해당 이동의 간격 추가",
                                           observation="; ".join(observations) or None, extra={"hierarchy": hier}))
                continue
            expected_pt, basis = pt, "기호 항목 '%s' 위: %s" % (h.get("marker"), why)
        elif kind == "note":
            expected_pt, basis = rc["note_above_pt"], "참고·주석('%s') 문장 위" % h.get("marker")
        elif kind == "body":
            pk = h.get("previous_structural_kind")
            if pk not in ("item", "bullet", "body"):
                results.append(make_result(doc, "PARA_SPACING", UNKNOWN, "일반 본문의 직전 문단이 항목·본문이 아니어서 간격 기준이 모호",
                                           target_of(p), role_of(p), current,
                                           unknown_reason="직전 문단 종류: %s" % (pk or "없음(문서 첫 문단)"),
                                           needed_info="일반 본문의 소속 관계 확인", extra={"hierarchy": hier}))
                continue
            expected_pt = rc["body_pt"]
            owner = h.get("owner_item")
            basis = "%s → 일반 본문%s" % (
                "항목" if pk in ("item", "bullet") else "일반 본문",
                " (소속: '%s' #%d)" % (owner.get("marker"), owner["paragraph"]) if owner else " (소속 항목 없음)")

        others = [a for a in allowed if abs(a - expected_pt) > tol]
        expected = {"value": expected_pt, "display": "문단 위 %gpt" % expected_pt + (" (또는 %s)" % "·".join("%gpt" % a for a in others) if others else ""),
                    "basis": basis}
        if others:
            expected["allowed"] = list(allowed)
        extra = {"hierarchy": hier}
        if current is None:
            results.append(make_result(doc, "PARA_SPACING", UNKNOWN, "문단 위 간격 값을 추출하지 못함",
                                       target_of(p), role_of(p), None, expected,
                                       unknown_reason="paraPr margin@prev 없음", needed_info="문단 모양 정의", extra=extra))
            continue
        ok = abs(cur_v - expected_pt) <= tol or any(abs(cur_v - a) <= tol for a in allowed)
        actions = None if ok else [{
            "op": "set_para_space_before", "para_pr_id": pf.get("para_pr_id"),
            "xml": "Contents/header.xml hh:paraPr[@id=%s]//hh:margin/hc:prev@value" % pf.get("para_pr_id"),
            "from": cur_v, "to": expected_pt,
            "note": ("목표값으로 설정(기존값에 더하지 않음, 반복 적용해도 누적 없음). "
                     "hp:switch case(HwpUnitChar)에는 pt×100, default 에는 그 2배 값이 기록되는 구조(샘플 전수 관찰)")}]
        results.append(make_result(doc, "PARA_SPACING", PASS if ok else FAIL,
                                   "문단 위 %gpt %s 기준 %s (%s)" % (cur_v, "=" if ok else "≠", expected["display"][5:], basis),
                                   target_of(p), role_of(p), current, expected, fix_actions=actions,
                                   observation="; ".join(observations) or None, extra=extra))
    return results + skip_group(doc, "PARA_SPACING", skipped)


# 번호·기호 뒤 공백 판정에 쓰는 공백 문자
WS_NAMES = {" ": "공백", "\t": "탭", "　": "전각 공백", " ": "묶음 빈칸", " ": "고정폭 빈칸"}


def _layout_trusted(doc):
    app = (((doc.r.get("package") or {}).get("version") or {}).get("application") or "")
    names = doc.rule_cfg("HANGING_INDENT").get("trusted_layout_applications") or []
    return any(n.lower() in app.lower() for n in names), app


def _indent_label(v):
    if v is None:
        return "-"
    if v < 0:
        return "내어쓰기 %gpt" % -v
    if v > 0:
        return "들여쓰기 %gpt" % v
    return "내어쓰기 없음(0pt)"


# 글자 수 근사(글꼴 파일이 없을 때)에서 '전각(1칸)' 으로 보는 문자 범위: 한글 음절·자모, 로마 숫자(Ⅰ), 원·괄호 숫자, 도형(□○ 등),
# 기타 기호, CJK 기호·전각 문자, ※, 한자
FULL_WIDTH_RANGES = ((0xAC00, 0xD7A3), (0x1100, 0x11FF), (0x3130, 0x318F), (0x2160, 0x217F), (0x2460, 0x24FF), (0x25A0, 0x25FF),
                     (0x2600, 0x26FF), (0x3000, 0x303F), (0x3200, 0x32FF), (0xFF01, 0xFF60), (0x203B, 0x203B),
                     (0x4E00, 0x9FFF))
# 한글(HWP)의 글꼴 언어 슬롯 대응(가정): 한글 음절·자모 → hangul, ASCII → latin, 한자 → hanja, 그 외 기호(□ ※ ① ․ 등) → symbol
SYMBOL_RANGES = ((0x2000, 0x2BFF), (0x3000, 0x303F), (0x3200, 0x33FF), (0xFF01, 0xFF60), (0x2E80, 0x2FFF))


def char_width_class(ch):
    """반환: ('full'|'half', 언어 슬롯) 또는 None(폭 기준 없음)."""
    o = ord(ch)
    if ch == "\t":
        return None
    if 0x20 <= o <= 0x7E or ch == "\u00a0":
        return "half", "latin"
    if 0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
        return "full", "hangul"
    if 0x4E00 <= o <= 0x9FFF:
        return "full", "hanja"
    if any(a <= o <= b for a, b in SYMBOL_RANGES):
        return "full", "symbol"
    if any(a <= o <= b for a, b in FULL_WIDTH_RANGES):
        return "full", "symbol"
    return None


def _width_rule_text(rc):
    we = rc.get("width_estimate") or {}
    return "글꼴 파일의 글자 폭 × 글자 크기 × 장평 + 자간(글꼴 없는 글자는 한글·기호 %g칸, 공백·숫자·영문 %g칸 근사)" % (
        we.get("full_em", 1.0), we.get("half_em", 0.5))


def estimate_prefix_width(doc, p, n, text=None):
    """문단 앞 n 글자(앞 공백 + 번호·기호 + 뒤 공백)의 폭(pt)을 계산한다.
    글자마다 자기 run 의 글자 모양(글꼴·크기·장평·자간)과 언어 슬롯을 쓴다.
    폭 = 글꼴 파일의 글자 폭(em) × 크기 × 장평/100 + 크기 × 자간/100 (HWP 자간은 글자 크기 대비 %).
    글꼴 파일이 없거나 글꼴에 글자가 없으면 글자 수 근사(1칸/0.5칸)로 대신하고 approx 목록에 남긴다.
    장평은 자동 수정 대상 글자 모양이면 수정 후 표준값, 글자 크기는 FONT_SIZE 규칙의 목표값, 자간은 실제값.
    반환: ((폭, 내역, approx 목록), None) 또는 (None, 사유)"""
    rc = doc.rule_cfg("HANGING_INDENT")
    we = rc.get("width_estimate") or {}
    em = {"full": we.get("full_em", 1.0), "half": we.get("half_em", 0.5)}
    visible = set(doc.visible_char_pr_ids(p))
    std_ratio = doc.rule_cfg("CHAR_RATIO").get("value")
    std_spacing = doc.rule_cfg("CHAR_SPACING").get("value")
    size_targets = doc.font_size_targets.get(p["index"]) or {}
    orig_text = p.get("text") or ""
    text = orig_text if text is None else text
    owner = []
    for run in p.get("runs") or []:
        owner.extend([run.get("char_pr_id")] * (run.get("chars") or 0))
    if text != orig_text and owner:
        # 번호·기호 교정으로 글자 수가 달라지면 앞 공백 뒤 글자들은 번호·기호 run 의 글자 모양을 따른다고 본다
        lead = len(text) - len(text.lstrip())
        if lead < len(owner):
            owner = owner[:lead] + [owner[lead]] * (len(text) - lead)
    if len(owner) < n:
        return None, "run 별 글자 수 정보가 문단 텍스트와 맞지 않음"
    total, parts, approx = 0.0, [], []
    for i, ch in enumerate(text[:n]):
        cls = char_width_class(ch)
        if cls is None:
            return None, "폭 기준이 없는 문자 '%s'(U+%04X)가 번호·기호 부분에 있음" % (ch.replace("\t", "탭"), ord(ch))
        kind, lang = cls
        cp = doc.char_pr(owner[i])
        if not cp:
            return None, "글자 모양 정의(charPr %s)가 없음" % owner[i]
        size = (cp.get("effective_size_pt") or {}).get(lang) or cp.get("size_pt")
        if owner[i] in size_targets:
            size = size_targets[owner[i]]   # 글자 크기 수정 후 기준
        if not size:
            return None, "글자 크기를 알 수 없음(charPr %s)" % owner[i]
        fixed = owner[i] in visible
        ratio = std_ratio if fixed and std_ratio is not None else (cp.get("ratio") or {}).get(lang, 100)
        spacing = std_spacing if fixed and std_spacing is not None else (cp.get("spacing") or {}).get(lang, 0)
        face = (cp.get("font_face") or {}).get(lang)
        font = doc.font_registry.get(face) if face else None
        adv = font.advance_em(ch) if font else None
        label = "공백" if ch == " " else ch
        if adv is None:
            adv = em[kind]
            why = ("글꼴 파일 없음: %s" % face) if font is None else ("글꼴에 글자 없음: %s" % face)
            if font is None and face:
                doc.missing_fonts.add(face)
            approx.append("%s(%s)" % (label, why))
            label += "*"
        w = size * adv * ratio / 100.0 + size * spacing / 100.0
        total += w
        parts.append("%s %gpt" % (label, round(w, 2)))
    return (total, parts, approx), None


def rule_hanging_indent(doc):
    rc = doc.rule_cfg("HANGING_INDENT")
    note_markers = tuple(rc.get("note_markers") or ())
    trusted, app = _layout_trusted(doc)
    results, skipped = [], []
    for p in doc.text_paragraphs():
        role = p["role"]["role"]
        text = p.get("text") or ""
        m = p.get("list_marker")
        marker = m["marker"] if m else detect_bullet(p)
        if not marker:
            marker = note_marker_of(text, note_markers)   # '*', '※' 주석 기호도 내어쓰기 대상(요구사항 12)
        mt = doc.marker_targets.get(p["index"])
        if mt and marker == mt[0]:
            # 번호·기호가 교정될 예정이면 교정 후 문자로 폭을 계산한다(Formatter 가 함께 바꾸므로)
            marker = mt[1]
            text = text.replace(mt[0], mt[1], 1)
        an = p.get("auto_numbering") or {}
        auto = not marker and an.get("type") in ("OUTLINE", "NUMBER", "BULLET")
        if not marker and not auto:
            continue
        if role in STRUCTURAL_SKIP_ROLES:
            skipped.append((p, STRUCTURAL_SKIP_ROLES[role]))
            continue
        pf = p.get("para_format") or {}

        # (1) 번호·기호 뒤 공백 한 칸 - 텍스트 문제이므로 자동 수정하지 않음
        if marker:
            s = text.lstrip()
            leading = len(text) - len(s)
            if s.startswith(marker):
                rest = s[len(marker):]
                ws = rest[: len(rest) - len(rest.lstrip(" \t　  "))]
                desc = "없음" if not ws else " + ".join(WS_NAMES.get(c, "U+%04X" % ord(c)) for c in ws)
                current = {"value": ws, "display": "'%s' 뒤 %s" % (marker, desc), "source": "문단 텍스트"}
                expected = {"value": " ", "display": "번호·기호·주석 기호 뒤 공백 한 칸", "basis": rc["spec_text"]}
                obs = "번호·기호 앞에 공백 %d칸(공백으로 들여쓴 것으로 보임)" % leading if leading else None
                ok = ws == " "
                results.append(make_result(
                    doc, "HANGING_INDENT", PASS if ok else FAIL,
                    "번호·기호 뒤 %s" % ("공백 한 칸" if ok else desc + " (표준: 공백 한 칸)"),
                    target_of(p), role_of(p), current, expected, observation=obs, fix_mode="manual_review",
                    recommendation=None if ok else "글자·공백을 추가·삭제하지 않는 원칙상 자동 수정 대상 아님. 작성자가 번호·기호 뒤 공백을 한 칸으로 정리",
                    extra={"sub_item": "번호·기호 뒤 공백"}))

        # (2) 둘째 줄 정렬 - 번호·기호 부분의 폭을 글자 수로 근사해 내어쓰기 값과 비교(줄 수와 무관하게 모든 항목 문단)
        ll = p.get("line_layout")
        sub = {"sub_item": "둘째 줄 정렬"}
        indent = pf.get("indent_pt")
        lines_note = ("줄 %d개(한글 저장 줄 배치 기준%s)" % (ll["line_count"], "" if trusted else ", 작성 프로그램이 한글이 아니라 참고만")
                      if ll else "줄 배치 정보 없음 - 줄 수와 관계없이 검사")
        current = {"value": {"indent_pt": indent, "left_margin_pt": pf.get("left_margin_pt")},
                   "display": _indent_label(indent),
                   "source": VALUE_SOURCE + " - paraPr/margin@intent", "refs": {"para_pr_id": pf.get("para_pr_id")},
                   "role_label": p["role"]["label"]}
        if rc.get("skip_single_line") and trusted and ll and ll.get("line_count") == 1:
            # 한 줄 문단은 둘째 줄이 없으므로 검사·수정하지 않음(2026-10-31 사용자 결정, sample_original_wannabe 기준)
            skipped.append((p, "한 줄 문단(한글 저장 줄 배치 기준) - 둘째 줄 정렬 검사 제외"))
            continue
        if auto:
            results.append(make_result(doc, "HANGING_INDENT", UNKNOWN, "자동 번호 문단의 번호 너비를 알 수 없음",
                                       target_of(p), role_of(p), current, None,
                                       unknown_reason="번호가 본문 텍스트에 없고 자동 번호 너비·정렬 설정의 해석이 확인되지 않음",
                                       needed_info="렌더링 결과 또는 자동 번호 너비", observation=lines_note, extra=sub))
            continue
        s = text.lstrip()
        prefix = text[: len(text) - len(s)] + marker
        s2 = s[len(marker):]
        prefix += s2[: len(s2) - len(s2.lstrip(" \t\u3000\u00a0\u2007"))]
        est, why = estimate_prefix_width(doc, p, len(prefix), text=text)
        if indent is None or est is None:
            results.append(make_result(doc, "HANGING_INDENT", UNKNOWN,
                                       "문단 들여쓰기 값을 추출하지 못함" if indent is None else "번호·기호 부분의 폭을 추정할 수 없음",
                                       target_of(p), role_of(p), current, None,
                                       unknown_reason="paraPr margin@intent 없음" if indent is None else why,
                                       needed_info="문단 모양 정의" if indent is None else "해당 문자의 폭 기준(설정 HANGING_INDENT.width_estimate)",
                                       observation=lines_note, extra=sub))
            continue
        width, parts, approx = est
        target = -round(width, 2)
        expected = {"value": {"indent_pt": target, "approximated": bool(approx)},
                    "display": "%s ('%s' 폭: %s)" % (_indent_label(target), prefix.replace("\t", "→"), " + ".join(parts)),
                    "basis": rc["spec_text"] + " / " + _width_rule_text(rc)}
        tol = (rc.get("width_estimate") or {}).get("tolerance_pt", 1.0)
        ok = abs(indent - target) <= tol
        reason = "내어쓰기 %s 기준 %s (허용 오차 ±%gpt)" % (
            _indent_label(indent), ("=" if ok else "≠") + " " + _indent_label(target), tol)
        if not ok and rc.get("keep_existing_hanging") and indent < 0:
            # 이미 내어쓰기가 있으면 폭과 관계없이 작성자 값을 존중(2026-10-31 사용자 결정, sample_original_wannabe 기준)
            ok = True
            reason = "내어쓰기 %s 가 이미 설정되어 있어 유지 (계산 폭 %s)" % (_indent_label(indent), _indent_label(target))
        actions = None if ok else [{
            "op": "set_para_hanging_indent", "para_pr_id": pf.get("para_pr_id"),
            "xml": "Contents/header.xml hh:paraPr[@id=%s]//hh:margin/hc:intent@value" % pf.get("para_pr_id"),
            "from": indent, "to": target,
            "note": "목표값으로 설정(누적 없음). hp:switch case 에는 pt×100, default 에는 그 2배 값. 왼쪽 여백은 바꾸지 않음"}]
        results.append(make_result(doc, "HANGING_INDENT", PASS if ok else FAIL, reason, target_of(p), role_of(p),
                                   current, expected, fix_actions=actions,
                                   observation="%s. %s" % (lines_note, ("글꼴 파일이 없어 글자 수 근사로 계산한 글자(*): " + ", ".join(approx))
                                                          if approx else "폭은 글꼴 파일의 글자 폭으로 계산"), extra=sub))
    reg = doc.font_registry
    if results:
        if doc.missing_fonts:
            results.append(make_result(doc, "HANGING_INDENT", UNKNOWN, "일부 글자의 폭을 글꼴 파일 없이 근사했음",
                                       unknown_reason="글꼴 폴더(%s)에 없는 글꼴: %s" % (reg.dir, ", ".join(sorted(doc.missing_fonts))),
                                       needed_info="해당 글꼴의 .ttf/.ttc 파일을 글꼴 폴더에 추가", extra={"sub_item": "글꼴 폭"}))
        elif reg.files:
            results.append(make_result(doc, "HANGING_INDENT", PASS, "모든 번호·기호 부분의 폭을 글꼴 파일로 계산",
                                       current={"value": reg.files, "display": "글꼴 파일 %d개: %s" % (len(reg.files), ", ".join(reg.files)), "source": str(reg.dir)},
                                       expected={"value": "font_metrics", "display": "글꼴 폭 기준", "basis": rc["spec_text"]},
                                       extra={"sub_item": "글꼴 폭"}))
    return results + skip_group(doc, "HANGING_INDENT", skipped)


# ----------------------------------------------------------------------------
# 문단 단위 규칙 (역할 기반)
# ----------------------------------------------------------------------------
def rule_font(doc):
    def current_fn(p):
        ids = doc.visible_char_pr_ids(p)
        fonts = {}
        for cid in ids:
            cp = doc.char_pr(cid)
            if cp:
                fonts[cid] = (cp.get("font_face") or {}).get("hangul")
        if not fonts:
            return None
        faces = sorted(set(fonts.values()), key=lambda f: (f is None, f))
        return {"value": faces[0] if len(faces) == 1 else faces,
                "display": ", ".join(str(f) for f in faces) + (" (혼재)" if len(faces) > 1 else ""),
                "source": VALUE_SOURCE + " - charPr/fontRef@hangul", "refs": {"char_pr_fonts": fonts}}

    def compare_fn(p, current, std):
        fonts = current["refs"]["char_pr_fonts"]
        expected = {"value": std, "display": std, "basis": doc.rule_cfg("FONT")["spec_text"]}
        bad = {cid: f for cid, f in fonts.items() if f != std}
        if bad:
            return FAIL, "한글 글꼴 %s ≠ 표준 %s" % (", ".join(sorted({str(f) for f in bad.values()})), std), expected, None
        return PASS, "한글 글꼴이 표준(%s)과 일치" % std, expected, None

    def actions_fn(p, current, std):
        fid = doc.font_id("hangul", std)
        return [{"op": "set_char_font", "char_pr_id": cid, "lang": "hangul",
                 "xml": "Contents/header.xml hh:charPr[@id=%d]/hh:fontRef@hangul" % cid,
                 "from": f, "to": std, "target_font_registered": fid is not None, "target_font_id": fid}
                for cid, f in current["refs"]["char_pr_fonts"].items() if f != std]

    return _role_paragraph_rule(doc, "FONT", current_fn, compare_fn, actions_fn)


def _char_metric_rule(doc, rule_id, key):
    rc = doc.rule_cfg(rule_id)
    std = rc.get("value")
    if std is None:
        return [make_result(doc, rule_id, SKIP, "%s은(는) 수정하지 않는 항목" % rc["name"],
                            unknown_reason=rc.get("spec_text") or "표준값 없음")]

    def current_fn(p):
        vals = {}
        for cid in doc.visible_char_pr_ids(p):
            cp = doc.char_pr(cid)
            if cp and cp.get(key):
                vals[cid] = {lang: cp[key].get(lang) for lang in COMPARED_LANGS}
        if not vals:
            return None
        distinct = sorted({v for d in vals.values() for v in d.values() if v is not None})
        return {"value": distinct[0] if len(distinct) == 1 else distinct,
                "display": ", ".join("%s%%" % v for v in distinct) + (" (혼재)" if len(distinct) > 1 else ""),
                "source": VALUE_SOURCE + " - charPr/%s@%s" % ("ratio" if key == "ratio" else "spacing", ",".join(COMPARED_LANGS)),
                "refs": {"char_pr_values": vals}}

    results = []
    for p in doc.text_paragraphs():
        cur = current_fn(p)
        if cur is None:
            results.append(make_result(doc, rule_id, UNKNOWN, "현재값을 추출하지 못함", target_of(p), role_of(p),
                                       unknown_reason="charPr 정의 없음", needed_info="글자 모양 정의"))
            continue
        expected = {"value": std, "display": "%d%%" % std, "basis": doc.rule_cfg(rule_id)["spec_text"]}
        bad = {cid: {l: v for l, v in d.items() if v != std} for cid, d in cur["refs"]["char_pr_values"].items()}
        bad = {cid: d for cid, d in bad.items() if d}
        if bad:
            actions = [{"op": "set_char_%s" % key, "char_pr_id": cid, "langs": sorted(d),
                        "xml": "Contents/header.xml hh:charPr[@id=%d]/hh:%s@%s" % (cid, key, ",".join(sorted(d))),
                        "from": d, "to": std} for cid, d in bad.items()]
            results.append(make_result(
                doc, rule_id, FAIL, "%s %s ≠ 표준 %d%% (역할과 무관하게 적용되는 항목)" % (
                    doc.rule_cfg(rule_id)["name"], cur["display"], std),
                target_of(p), role_of(p), cur, expected, fix_actions=actions))
        else:
            results.append(make_result(doc, rule_id, PASS, "%s이 표준(%d%%)과 일치" % (doc.rule_cfg(rule_id)["name"], std),
                                       target_of(p), role_of(p), cur, expected))
    return results


def rule_char_ratio(doc):
    return _char_metric_rule(doc, "CHAR_RATIO", "ratio")


def rule_char_spacing(doc):
    return _char_metric_rule(doc, "CHAR_SPACING", "spacing")


def rule_line_spacing(doc):
    def current_fn(p):
        pf = p.get("para_format") or {}
        if pf.get("line_spacing_type") is None:
            return None
        t, v = pf["line_spacing_type"], pf["line_spacing_value"]
        return {"value": {"type": t, "value": v}, "display": pf.get("line_spacing") or "%s %s" % (t, v),
                "source": VALUE_SOURCE + " - paraPr/lineSpacing", "refs": {"para_pr_id": pf.get("para_pr_id")}}

    rc = doc.rule_cfg("LINE_SPACING")
    undecided_note = rc.get("note_after_object_undecided")

    def after_object(p):
        h = p.get("hierarchy") or {}
        prev = h.get("previous_paragraph")
        return prev is not None and (doc.paragraphs[prev].get("hierarchy") or {}).get("kind") == "object"

    def compare_fn(p, current, std):
        expected = {"value": {"type": "PERCENT", "value": std}, "display": "%d%%" % std,
                    "basis": rc["spec_text"]}
        if undecided_note and p["role"]["role"] == "note" and after_object(p):
            return UNKNOWN, "표/그림 바로 아래의 참고·주석", expected, undecided_note
        cv = current["value"]
        if cv["type"] != "PERCENT":
            return FAIL, "줄 간격 종류가 '%s'(%s)로 표준의 비율(%%) 방식과 다름" % (
                LINE_TYPE_LABEL.get(cv["type"], cv["type"]), current["display"]), expected, None
        lo_hi = (rc.get("allowed_range_by_role") or {}).get(p.get("spec_role") or p["role"]["role"])
        if lo_hi and cv["value"] != std:
            # 허용 범위(2026-10-31 사용자 결정): 범위 안이면 적합, 벗어나면 표준값으로 수정
            expected["display"] = "%d%% (%d~%d%% 허용)" % (std, lo_hi[0], lo_hi[1])
            expected["allowed_range"] = list(lo_hi)
            if lo_hi[0] <= cv["value"] <= lo_hi[1]:
                return PASS, "줄 간격 %d%% 가 허용 범위 %d~%d%% 안" % (cv["value"], lo_hi[0], lo_hi[1]), expected, None
            return FAIL, "줄 간격 %d%% 가 허용 범위 %d~%d%% 밖 (표준 %d%%)" % (cv["value"], lo_hi[0], lo_hi[1], std), expected, None
        if cv["value"] != std:
            return FAIL, "줄 간격 %d%% ≠ 표준 %d%%" % (cv["value"], std), expected, None
        return PASS, "줄 간격이 표준(%d%%)과 일치" % std, expected, None

    def actions_fn(p, current, std):
        pid = current["refs"]["para_pr_id"]
        return [{"op": "set_para_line_spacing", "para_pr_id": pid,
                 "xml": "Contents/header.xml hh:paraPr[@id=%s]//hh:lineSpacing" % pid,
                 "from": current["value"], "to": {"type": "PERCENT", "value": std},
                 "note": "hp:switch 의 case/default 양쪽 lineSpacing 을 함께 갱신해야 함(구조 분석 결과)"}]

    return _role_paragraph_rule(doc, "LINE_SPACING", current_fn, compare_fn, actions_fn)


NUMERIC_CELL = re.compile(r"^[\s\d,.\-+%()/:~₩$원]+$")


def rule_para_align(doc):
    rc = doc.rule_cfg("PARA_ALIGN")

    def current_fn(p):
        pf = p.get("para_format") or {}
        if not pf.get("align"):
            return None
        return {"value": pf["align"], "display": ALIGN_LABEL.get(pf["align"], pf["align"]),
                "source": VALUE_SOURCE + " - paraPr/align@horizontal", "refs": {"para_pr_id": pf.get("para_pr_id")}}

    def compare_fn(p, current, std):
        expected = {"value": std, "display": ALIGN_LABEL.get(std, std), "basis": rc["spec_text"]}
        if p["role"]["role"] == "table_cell":
            t = doc.tables.get(p["context"].get("table_id")) or {}
            if rc.get("single_cell_table_undecided") and t.get("rows") == 1 and t.get("cols") == 1:
                return UNKNOWN, "1×1 표 안의 문단", expected, \
                    "1×1 표는 글상자·박스 용도일 수 있어 '표 내부 항목'에 해당하는지 판단할 수 없음"
            text = (p.get("text") or "").strip()
            if any(ch.isdigit() for ch in text) and NUMERIC_CELL.match(text):
                allowed = rc.get("numeric_cell_allowed") or []
                exp = {"value": allowed, "display": "%s 허용" % "·".join(ALIGN_LABEL.get(a, a) for a in allowed),
                       "basis": rc["spec_text"]}
                if current["value"] in allowed:
                    return PASS, "숫자 셀 정렬 %s 는 허용 범위(가운데·오른쪽)" % current["display"], exp, None
                return FAIL, "숫자 셀 정렬 %s ≠ 허용 범위(가운데·오른쪽) → 가운데로 수정" % current["display"], expected, None
        if current["value"] != std:
            return FAIL, "정렬 %s ≠ 표준 %s" % (current["display"], expected["display"]), expected, None
        return PASS, "정렬이 표준(%s)과 일치" % expected["display"], expected, None

    def actions_fn(p, current, std):
        pid = current["refs"]["para_pr_id"]
        return [{"op": "set_para_align", "para_pr_id": pid,
                 "xml": "Contents/header.xml hh:paraPr[@id=%s]/hh:align@horizontal" % pid,
                 "from": current["value"], "to": std}]

    return _role_paragraph_rule(doc, "PARA_ALIGN", current_fn, compare_fn, actions_fn)


# ----------------------------------------------------------------------------
# 텍스트 규칙 (검사만 - 자동 수정 대상 아님)
# ----------------------------------------------------------------------------
NUMBER_SKIP_ROLES = {"heading_1": "제목 번호", "heading_2": "제목 번호", "heading_3": "제목 번호",
                     "document_title": "문서 제목"}


HANGUL_SEQ = "가나다라마바사아자차카타파하"
CIRCLED_DIGITS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
CIRCLED_HANGUL = "㉮㉯㉰㉱㉲㉳㉴㉵㉶㉷㉸㉹㉺㉻"


def expected_number_marker(kind, n, sample):
    """같은 단계의 n번째(1부터) 항목 번호 표기. 마침표·괄호는 현재 표기의 문자를 따른다. 범위를 넘으면 None."""
    tail = sample[-1] if sample else ""
    if kind == "digit_dot":
        return "%d%s" % (n, tail if tail in ".．" else ".")
    if kind == "digit_paren":
        return "%d%s" % (n, tail if tail in ")）" else ")")
    if kind == "hangul_dot":
        return (HANGUL_SEQ[n - 1] + (tail if tail in ".．" else ".")) if n <= len(HANGUL_SEQ) else None
    if kind == "hangul_paren":
        return (HANGUL_SEQ[n - 1] + (tail if tail in ")）" else ")")) if n <= len(HANGUL_SEQ) else None
    if kind == "circled_digit":
        return CIRCLED_DIGITS[n - 1] if n <= len(CIRCLED_DIGITS) else None
    if kind == "circled_hangul":
        return CIRCLED_HANGUL[n - 1] if n <= len(CIRCLED_HANGUL) else None
    return None


def _marker_action(doc, p, old, new, what):
    return {"op": "replace_marker", "paragraph_index": p["index"], "from": old, "to": new,
            "xml": "Contents/section%d.xml %s 의 hp:t 첫 글자 '%s' → '%s'" % (p.get("section", 0), p["path"], old, new),
            "note": "텍스트 불변 원칙의 예외: %s 문자만 바꾼다(그 밖의 글자·공백은 그대로)" % what}


def rule_item_number(doc):
    """항목 번호(요구사항 + 2026-10-31 결정): 표준 체계 여부와 같은 단계 안의 순서(1부터, 상위 단계가 바뀌면 다시 1부터)를 검사하고
    번호 문자를 교정한다. 본문 최상위의 텍스트 번호 항목만 대상(자동 번호·표 안은 제외)."""
    rc = doc.rule_cfg("ITEM_NUMBER")
    allowed = set(rc["allowed_kinds"])
    ex = rc["kind_examples"]
    results, skipped = [], []
    counters = {}   # 단계 -> 현재 번호
    for p in doc.paragraphs:
        h = p.get("hierarchy") or {}
        kind = h.get("kind")
        if kind == "item":
            level = h["level"]
            for lv in list(counters):
                if lv > level:
                    del counters[lv]
            counters[level] = counters.get(level, 0) + 1
        m = p.get("list_marker")
        if not m or m.get("type") != "number":
            continue
        role = p["role"]["role"]
        if role in STRUCTURAL_SKIP_ROLES:
            skipped.append((p, STRUCTURAL_SKIP_ROLES[role]))
            continue
        current = {"value": m["marker"], "display": "'%s' (%s 형식)" % (m["marker"], ex.get(m["kind"], m["kind"])),
                   "source": "문단 텍스트(번호는 본문 텍스트에 포함된 문자)"}
        expected = {"value": rc["allowed_kinds"], "display": rc["spec_text"], "basis": rc["spec_text"]}
        if m["kind"] not in allowed:
            results.append(make_result(doc, "ITEM_NUMBER", FAIL, "번호 형식 '%s' 는 표준 번호 체계(1. 가. 1) 가) ① ㉮)에 없음" % ex.get(m["kind"], m["kind"]),
                                       target_of(p), role_of(p), current, expected, fix_mode="manual_review",
                                       recommendation="표준 체계 밖 표기는 자동으로 바꾸지 않음. 작성자가 1. 가. 1) 가) ① ㉮ 체계로 정리"))
            continue
        if kind != "item":
            skipped.append((p, "본문 최상위 항목이 아님(표·차례·글상자 등) - 번호 순서 검사 대상 아님"))
            continue
        n = counters[h["level"]]
        exp = expected_number_marker(m["kind"], n, m["marker"])
        if exp is None:
            results.append(make_result(doc, "ITEM_NUMBER", UNKNOWN, "%d번째 항목의 표준 표기가 없음" % n, target_of(p), role_of(p), current,
                                       unknown_reason="해당 번호 체계의 표기 범위를 넘음", needed_info="표기 범위 확장"))
            continue
        expected = {"value": exp, "display": "'%s' (%d단계 %d번째)" % (exp, h["level"], n), "basis": rc["spec_text"]}
        if m["marker"] == exp:
            results.append(make_result(doc, "ITEM_NUMBER", PASS, "번호 '%s' 가 순서(%d번째)에 맞음" % (exp, n),
                                       target_of(p), role_of(p), current, expected))
        else:
            doc.marker_targets[p["index"]] = (m["marker"], exp)
            results.append(make_result(doc, "ITEM_NUMBER", FAIL, "번호 '%s' ≠ 순서상 '%s' (%d단계 %d번째)" % (m["marker"], exp, h["level"], n),
                                       target_of(p), role_of(p), current, expected,
                                       fix_actions=[_marker_action(doc, p, m["marker"], exp, "항목 번호")]))
    return results + skip_group(doc, "ITEM_NUMBER", skipped)


# 표준 기호와 혼동되기 쉬운 문자 (사유 설명용)
BULLET_LOOKALIKES = {
    "o": "영문 소문자 o (표준은 한글 자모 ㅇ)",
    "O": "영문 대문자 O (표준은 한글 자모 ㅇ)",
    "○": "원 기호 U+25CB (표준은 한글 자모 ㅇ)",
    "◦": "흰 점 U+25E6 (표준은 한글 자모 ㅇ)",
    "·": "가운뎃점 U+00B7 (표준은 ․ U+2024)",
    "ㆍ": "한글 아래아 U+318D (표준은 ․ U+2024)",
    "‧": "하이픈 점 U+2027 (표준은 ․ U+2024)",
    "∙": "불릿 연산자 U+2219 (표준은 ․ U+2024)",
    "•": "불릿 U+2022 (표준은 ․ U+2024)",
    "–": "en dash U+2013 (표준은 하이픈 -)",
    "—": "em dash U+2014 (표준은 하이픈 -)",
    "■": "검은 사각형 U+25A0 (표준은 □ U+25A1)",
    "☐": "투표 상자 U+2610 (표준은 □ U+25A1)",
}
# 혼동 문자 -> 의도로 보이는 표준 기호 (보고서 제안용, 자동 치환하지 않음)
BULLET_SUGGEST = {"o": "ㅇ", "O": "ㅇ", "○": "ㅇ", "◦": "ㅇ", "·": "․", "ㆍ": "․", "‧": "․", "∙": "․", "•": "․",
                  "–": "-", "—": "-", "■": "□", "☐": "□"}
def rule_item_bullet(doc):
    """항목 기호(요구사항 4 + 2026-10-31 결정): 표준 기호 여부와 층위 순서(□ → ㅇ → - → ․)를 검사하고 기호 문자를 교정한다.
    층위 규칙: 번호 항목·다른 문단 뒤의 첫 기호는 □, 아래로는 한 층위씩만(건너뛰면 바로 아래 층위 기호), 위로는 어느 층위든 복귀 가능."""
    rc = doc.rule_cfg("ITEM_BULLET")
    allowed = rc["allowed"]
    results, skipped = [], []
    stack = []   # 열려 있는 기호 층위(본문 최상위 체인)
    for p in doc.paragraphs:
        h = p.get("hierarchy") or {}
        kind = h.get("kind")
        if kind in ("item", "other", "object", "unmapped_item"):
            stack = []
        b = detect_bullet(p)
        if not b:
            continue
        role = p["role"]["role"]
        if role in STRUCTURAL_SKIP_ROLES:
            skipped.append((p, STRUCTURAL_SKIP_ROLES[role]))
            continue
        current = {"value": b, "display": "%s (U+%04X)" % (b, ord(b[0])), "source": "문단 텍스트(기호는 본문 텍스트에 포함된 문자)"}
        expected = {"value": allowed, "display": " → ".join(allowed), "basis": rc["spec_text"]}
        if b not in allowed:
            if b in BULLET_SUGGEST:
                expected["suggested"] = BULLET_SUGGEST[b]
                expected["display"] = "%s (표준 기호 %s)" % (BULLET_SUGGEST[b], " → ".join(allowed))
            why = BULLET_LOOKALIKES.get(b, "표준 기호(□ ㅇ - ․)에 없는 문자")
            results.append(make_result(doc, "ITEM_BULLET", FAIL, "기호 '%s': %s" % (b, why), target_of(p), role_of(p), current, expected,
                                       fix_mode="manual_review", recommendation="표준 밖 기호는 자동으로 바꾸지 않음. 작성자가 표준 기호로 정리"))
            continue
        if kind != "bullet":
            results.append(make_result(doc, "ITEM_BULLET", PASS, "기호 '%s' 는 표준 기호에 포함(층위 순서는 본문 최상위 항목만 검사)" % b,
                                       target_of(p), role_of(p), current, expected))
            continue
        level = allowed.index(b) + 1
        if not stack:
            exp_level = 1
        elif level > stack[-1] + 1:
            exp_level = stack[-1] + 1
        else:
            exp_level = level
        while stack and stack[-1] >= exp_level:
            stack.pop()
        stack.append(exp_level)
        exp = allowed[exp_level - 1]
        expected = {"value": exp, "display": "'%s' (%d층위)" % (exp, exp_level), "basis": rc["spec_text"]}
        if exp == b:
            results.append(make_result(doc, "ITEM_BULLET", PASS, "기호 '%s' 가 층위 순서(%d층위)에 맞음" % (b, exp_level),
                                       target_of(p), role_of(p), current, expected))
        else:
            why = "첫 기호는 □" if exp_level == 1 else "'%s' 다음 층위는 '%s'(건너뛸 수 없음)" % (allowed[exp_level - 2], exp)
            doc.marker_targets[p["index"]] = (b, exp)
            results.append(make_result(doc, "ITEM_BULLET", FAIL, "기호 '%s' ≠ 층위상 '%s' (%s)" % (b, exp, why),
                                       target_of(p), role_of(p), current, expected,
                                       fix_actions=[_marker_action(doc, p, b, exp, "항목 기호")]))
    return results + skip_group(doc, "ITEM_BULLET", skipped)


DATE_NUMERIC = re.compile(r"(?<![\d.])((?:19|20)\d{2}|['’‘]\d{2})\s*([.\-/])\s*(\d{1,2})\s*\2\s*(\d{1,2})(?!\d)(\s*\.)?")
DATE_KOREAN = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")


def _canonical_date(year, month, day):
    y = year if year[0].isdigit() else "'" + year[1:]
    return "%s. %d. %d." % (y, int(month), int(day))


def find_dates(text):
    out = []
    for m in DATE_NUMERIC.finditer(text):
        mo, d = int(m.group(3)), int(m.group(4))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            continue
        raw = m.group(0)
        canon = _canonical_date(m.group(1), m.group(3), m.group(4))
        out.append({"raw": raw, "start": m.start(), "canonical": canon, "kind": "numeric"})
    for m in DATE_KOREAN.finditer(text):
        mo, d = int(m.group(2)), int(m.group(3))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            continue
        out.append({"raw": m.group(0), "start": m.start(),
                    "canonical": _canonical_date(m.group(1), m.group(2), m.group(3)), "kind": "korean"})
    return sorted(out, key=lambda x: x["start"])


def rule_date_format(doc):
    rc = doc.rule_cfg("DATE_FORMAT")
    results = []
    for p in doc.text_paragraphs():
        for d in find_dates(p.get("text") or ""):
            current = {"value": d["raw"], "display": d["raw"], "source": "문단 텍스트", "refs": {"offset": d["start"]}}
            expected = {"value": d["canonical"], "display": d["canonical"], "basis": rc["spec_text"]}
            if d["raw"] == d["canonical"]:
                results.append(make_result(doc, "DATE_FORMAT", PASS, "날짜 표기가 표준 형식과 일치",
                                           target_of(p), role_of(p), current, expected))
            else:
                why = "한글 연월일 표기" if d["kind"] == "korean" else "점 뒤 띄어쓰기·끝 마침표·앞자리 0 등 표기 차이"
                results.append(make_result(doc, "DATE_FORMAT", FAIL, "날짜 '%s' ≠ 표준 형식 '%s' (%s)" % (d["raw"], d["canonical"], why),
                                           target_of(p), role_of(p), current, expected))
    return results


TIME_AMPM = re.compile(r"(오전|오후|AM|PM|am|pm|a\.m\.|p\.m\.)\s*(\d{1,2})\s*(?::\s*(\d{2})|시)")
TIME_COLON = re.compile(r"(?<![\d:.])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")
TIME_KOREAN = re.compile(r"(?<![\d:])(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?(?=\s|$|[~\-–,.)\]]|까지|부터|에|경|정각)")


def find_times(text):
    out, taken = [], []
    for m in TIME_AMPM.finditer(text):
        out.append({"raw": m.group(0), "start": m.start(), "kind": "ampm"})
        taken.append((m.start(), m.end()))

    def free(m):
        return not any(a <= m.start() < b for a, b in taken)

    for m in TIME_COLON.finditer(text):
        if free(m):
            out.append({"raw": m.group(0), "start": m.start(), "kind": "colon"})
    for m in TIME_KOREAN.finditer(text):
        if free(m):
            out.append({"raw": m.group(0), "start": m.start(), "kind": "korean"})
    return sorted(out, key=lambda x: x["start"])


def rule_time_format(doc):
    rc = doc.rule_cfg("TIME_FORMAT")
    results = []
    for p in doc.text_paragraphs():
        for t in find_times(p.get("text") or ""):
            current = {"value": t["raw"], "display": t["raw"], "source": "문단 텍스트", "refs": {"offset": t["start"]}}
            expected = {"value": "HH:MM (24시간제)", "display": "24시간제 HH:MM (예: 14:50)", "basis": rc["spec_text"]}
            if t["kind"] == "colon":
                results.append(make_result(doc, "TIME_FORMAT", PASS, "24시간제 HH:MM 표기",
                                           target_of(p), role_of(p), current, expected))
            elif t["kind"] == "ampm":
                results.append(make_result(doc, "TIME_FORMAT", FAIL, "오전/오후(12시간제) 표기",
                                           target_of(p), role_of(p), current, expected))
            else:
                results.append(make_result(doc, "TIME_FORMAT", UNKNOWN, "'N시' 표기는 명세 예시(14:50)와 형식이 다름",
                                           target_of(p), role_of(p), current, expected,
                                           unknown_reason="명세는 '24시간제'와 예시만 제시해 'N시 N분' 표기의 허용 여부가 불명확",
                                           needed_info="'14시 50분' 형식 허용 여부"))
    return results


def _blank_run_before(doc, p):
    """문단 바로 앞에 이어진 빈 문단(본문 최상위, 텍스트·개체·컨트롤 없음)들의 index(앞쪽부터)."""
    out = []
    i = p["index"] - 1
    while i >= 0:
        q = doc.paragraphs[i]
        ctx = q["context"]
        if not (q.get("is_empty") and ctx.get("container") == "body" and ctx.get("depth", 0) == 0
                and not q.get("objects") and not q.get("controls")):
            break
        out.append(i)
        i -= 1
    return list(reversed(out))


def rule_page_break_heading(doc):
    """대제목 쪽 나눔(2026-10-31 사용자 결정, A 규칙): 쪽 마지막 줄이거나 뒤 문단이 다음 쪽이면, 또는 이미 쪽 맨 위면 쪽 나눔.
    쪽 위치는 한글이 저장한 줄 배치 캐시로 판단한다. 첫 대제목은 제외."""
    rc = doc.rule_cfg("PAGE_BREAK_HEADING")
    trusted, app = _layout_trusted(doc)
    heads = [p for p in doc.paragraphs if (p.get("hierarchy") or {}).get("kind") == "item" and p["hierarchy"].get("level") == 1]
    results = []
    for n, p in enumerate(heads):
        has = bool(p.get("page_break_before"))
        current = {"value": has, "display": "쪽 나눔 %s" % ("있음" if has else "없음"), "source": VALUE_SOURCE + " - hp:p@pageBreak"}
        expected = {"value": True, "display": "새 쪽에서 시작(쪽 나눔)", "basis": rc["spec_text"]}
        ll = p.get("line_layout") or {}
        if n == 0:
            results.append(make_result(doc, "PAGE_BREAK_HEADING", SKIP, "문서의 첫 대제목은 쪽 나눔 대상이 아님",
                                       target_of(p), role_of(p), unknown_reason="첫 대제목 제외(규칙)"))
            continue
        if has:
            results.append(make_result(doc, "PAGE_BREAK_HEADING", PASS, "쪽 나눔이 있어 새 쪽에서 시작", target_of(p), role_of(p),
                                       current, expected))
            continue
        if not trusted or ll.get("page") is None:
            results.append(make_result(doc, "PAGE_BREAK_HEADING", UNKNOWN, "쪽 위치를 판단할 줄 배치 정보가 없음", target_of(p), role_of(p),
                                       current, expected,
                                       unknown_reason=("줄 배치 캐시(hp:linesegarray) 없음" if trusted else "작성 프로그램(%s)이 한글이 아니라 줄 배치 캐시를 신뢰하지 않음" % app),
                                       needed_info="한글에서 저장한 파일(줄 배치 캐시) 또는 쪽 위치 확인"))
            continue
        where = ("이미 쪽 맨 위(%d쪽 첫 문단) - 자리 고정" % ll["page"] if ll.get("page_first")
                 else "쪽 마지막(뒤 문단이 다음 쪽) - %d쪽" % ll["page"] if ll.get("page_last") else None)
        if ll.get("page_last") is None and not ll.get("page_first"):
            results.append(make_result(doc, "PAGE_BREAK_HEADING", UNKNOWN, "뒤 문단의 쪽 위치를 알 수 없음", target_of(p), role_of(p),
                                       current, expected, unknown_reason="마지막 최상위 문단이라 다음 문단과 비교할 수 없음",
                                       needed_info="쪽 위치 확인"))
            continue
        if where is None:
            results.append(make_result(doc, "PAGE_BREAK_HEADING", PASS, "쪽 중간(%d쪽)에 있고 뒤 내용이 같은 쪽에 이어짐" % ll["page"],
                                       target_of(p), role_of(p), current, {"value": False, "display": "쪽 나눔 불필요", "basis": rc["spec_text"]}))
            continue
        si = p.get("section", 0)
        actions = [{"op": "set_page_break", "paragraph_index": p["index"], "from": False, "to": True,
                    "xml": "Contents/section%d.xml hp:p@pageBreak (%s)" % (si, p["path"]),
                    "note": "문단 속성만 바꿈. 줄 배치는 한글이 열 때 다시 계산"}]
        # 빈 문단은 이미 쪽 맨 위로 밀린 대제목 앞에서만 지운다. 쪽 마지막 줄 대제목 앞의 빈 문단은 남긴다
        # (2026-10-31 사용자 결정, sample_original_wannabe 기준)
        blanks = (_blank_run_before(doc, p) if rc.get("delete_blank_before")
                  and (ll.get("page_first") or not rc.get("delete_blank_only_page_first")) else [])
        for j in blanks:
            actions.append({"op": "delete_empty_paragraph", "paragraph_index": j, "from": "빈 문단", "to": "삭제",
                            "xml": "Contents/section%d.xml hp:p 삭제 (%s)" % (si, doc.paragraphs[j]["path"]),
                            "note": "절대 원칙 예외: 쪽 나눔 대제목 바로 앞의 빈 문단(텍스트·개체·컨트롤 없음)만 삭제"})
        results.append(make_result(doc, "PAGE_BREAK_HEADING", FAIL, "대제목이 %s → 쪽 나눔 필요%s" % (
            where, " (바로 앞 빈 문단 %d개 삭제)" % len(blanks) if blanks else ""),
            target_of(p), role_of(p), current, expected, fix_actions=actions,
            observation="쪽 위치는 수정 전 줄 배치 캐시 기준", extra={"blank_paragraphs_to_delete": blanks}))
    return results


# ----------------------------------------------------------------------------
# 표 단위 규칙
# ----------------------------------------------------------------------------
def rule_table_side_border(doc):
    """표 양단선(요구사항 10): 표의 양 끝 열에 있는 셀의 왼쪽/오른쪽 테두리선이 없어야 한다."""
    rc = doc.rule_cfg("TABLE_SIDE_BORDER")
    none_t = rc.get("none_type", "NONE")
    boxes = {t["id"] for t, _ in _title_boxes(doc)}
    results = []
    for t in doc.r.get("tables") or []:
        target = {"type": "table", "table_id": t["id"], "path": t.get("path"), "paragraph_index": t.get("anchor_paragraph")}
        cols = t.get("cols") or 0
        expected = {"value": none_t, "display": "왼쪽·오른쪽 끝 선 없음(NONE)", "basis": rc["spec_text"]}
        if t["id"] in boxes:
            results.append(make_result(doc, "TABLE_SIDE_BORDER", SKIP, "문서 제목 박스(양단선 1mm 규칙 대상)라 표 양단선 규칙에서 제외",
                                       target, unknown_reason="문서 제목 박스는 '문서 제목 박스' 규칙(양단선 1mm)의 대상"))
            continue
        if not cols or not t.get("cells"):
            results.append(make_result(doc, "TABLE_SIDE_BORDER", UNKNOWN, "표 열 수·셀 정보 없음", target, expected=expected,
                                       unknown_reason="hp:tbl@colCnt 또는 hp:tc 미발견", needed_info="표 구조"))
            continue
        if t.get("rows") == 1 and cols == 1:
            results.append(make_result(doc, "TABLE_SIDE_BORDER", UNKNOWN, "1×1 표", target, expected=expected,
                                       unknown_reason="1×1 표는 글상자·박스 용도일 수 있어 일반 표의 양단선 규칙 대상인지 판단할 수 없음",
                                       needed_info="1×1 표의 용도 확인"))
            continue
        edge_cells, bad, missing = [], [], []
        for c in t["cells"]:
            sides = []
            if c.get("col") == 0:
                sides.append("left")
            if c.get("col") is not None and c["col"] + (c.get("col_span") or 1) == cols:
                sides.append("right")
            if not sides:
                continue
            edge_cells.append(c)
            borders = c.get("borders")
            if not borders:
                missing.append(c)
                continue
            wrong = {s: (borders.get(s) or {}).get("type") for s in sides if (borders.get(s) or {}).get("type") != none_t}
            if wrong:
                bad.append((c, wrong))
        if missing:
            results.append(make_result(doc, "TABLE_SIDE_BORDER", UNKNOWN, "일부 셀의 테두리 정의(borderFill)를 찾지 못함", target,
                                       expected=expected, unknown_reason="셀 %s 의 borderFillIDRef 정의 없음"
                                       % ", ".join("%d행 %d열" % ((c.get("row") or 0) + 1, (c.get("col") or 0) + 1) for c in missing[:5]),
                                       needed_info="header.xml 의 hh:borderFill 정의"))
            continue
        current = {"value": [{"row": c["row"], "col": c["col"], "sides": wrong} for c, wrong in bad],
                   "display": "양 끝 열 셀 %d개 중 %d개에 끝 선 있음" % (len(edge_cells), len(bad)) if bad else "양 끝 열 셀 %d개 모두 끝 선 없음" % len(edge_cells),
                   "source": VALUE_SOURCE + " - hp:tc@borderFillIDRef → hh:borderFill/leftBorder·rightBorder@type"}
        if not bad:
            results.append(make_result(doc, "TABLE_SIDE_BORDER", PASS, "표 양단선 없음", target, current=current, expected=expected))
            continue
        groups = OrderedDict()
        for c, wrong in bad:
            g = groups.setdefault((c.get("border_fill_id"), tuple(sorted(wrong))), {"from": {}, "cells": []})
            g["from"].update(wrong)
            g["cells"].append({"table_id": t["id"], "row": c["row"], "col": c["col"]})
        actions = [{"op": "set_cell_border", "border_fill_id": bf, "sides": list(sides), "to": none_t, "from": g["from"],
                    "cells": g["cells"],
                    "xml": "Contents/header.xml hh:borderFill[@id=%s]/hh:%s@type" % (bf, "|".join("%sBorder" % s for s in sides)),
                    "note": "셀이 쓰는 borderFill 을 복제해 해당 셀만 새 정의를 참조하게 함(다른 셀·표의 테두리는 그대로)"}
                   for (bf, sides), g in groups.items()]
        results.append(make_result(doc, "TABLE_SIDE_BORDER", FAIL,
                                   "표 양 끝 열 셀 %d개에 왼쪽/오른쪽 테두리선이 있음(표준: 없음)" % len(bad),
                                   target, current=current, expected=expected, fix_actions=actions,
                                   observation="표 자체의 borderFill(hp:tbl@borderFillIDRef)은 셀 선 표시에 쓰이지 않는 것으로 보아 바꾸지 않음"))
    return results


# ----------------------------------------------------------------------------
# 레지스트리: (규칙 ID, 함수). 순서가 보고서 순서가 된다.
# ----------------------------------------------------------------------------
RULES = [
    ("DOC_TITLE_BOX", rule_doc_title_box),
    ("PAPER_SIZE", rule_paper_size),
    ("PAPER_ORIENTATION", rule_paper_orientation),
    ("PAGE_MARGIN", rule_page_margin),
    ("HEADER_FOOTER_MARGIN", rule_header_footer_margin),
    ("PAGE_NUMBER", rule_page_number),
    ("WORD_BREAK", rule_word_break),
    ("FONT", rule_font),
    ("FONT_SIZE", rule_font_size),
    ("CHAR_RATIO", rule_char_ratio),
    ("CHAR_SPACING", rule_char_spacing),
    ("LINE_SPACING", rule_line_spacing),
    ("PARA_ALIGN", rule_para_align),
    ("PARA_SPACING", rule_para_spacing),
    ("PAGE_BREAK_HEADING", rule_page_break_heading),
    ("ITEM_NUMBER", rule_item_number),
    ("ITEM_BULLET", rule_item_bullet),
    ("HANGING_INDENT", rule_hanging_indent),
    ("TABLE_SIDE_BORDER", rule_table_side_border),
    ("DATE_FORMAT", rule_date_format),
    ("TIME_FORMAT", rule_time_format),
]

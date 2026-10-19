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

import re
from collections import Counter, defaultdict

PASS, FAIL, UNKNOWN, SKIP = "PASS", "FAIL", "UNKNOWN", "SKIP"

FIX_MODE_BY_SPEC = {
    "자동 수정": "auto",
    "분석 후 수정": "auto_if_role_confirmed",
    "검사만": "manual_review",
    "분석만": "none",
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

    def __init__(self, inspection, config):
        self.r = inspection
        self.cfg = config
        self.paragraphs = inspection.get("paragraphs") or []
        self.tables = {t["id"]: t for t in inspection.get("tables") or []}
        fd = inspection.get("format_definitions") or {}
        self.char_props = fd.get("char_properties") or {}
        self.para_props = fd.get("para_properties") or {}
        self.fonts = fd.get("fonts") or {}
        self.sections = (inspection.get("document") or {}).get("sections") or []
        self.role_min = config.get("role_confidence_min", 0.6)

    def rule_cfg(self, rule_id):
        return self.cfg["rules"][rule_id]

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


def role_of(p):
    r = p["role"]
    out = {"role": r["role"], "label": r["label"], "confidence": r["confidence"], "source": ROLE_SOURCE}
    if r.get("unclassified_reason"):
        out["unclassified_reason"] = r["unclassified_reason"]
    return out


def make_result(doc, rule_id, status, reason, target=None, role=None, current=None, expected=None,
                fix_actions=None, unknown_reason=None, needed_info=None, observation=None, extra=None):
    rc = doc.rule_cfg(rule_id)
    mode = FIX_MODE_BY_SPEC.get(rc["spec_mode"], "none")
    fix = {"mode": mode, "possible": False, "recommendation": None, "actions": []}
    if status == FAIL:
        if mode in ("auto", "auto_if_role_confirmed") and fix_actions:
            fix["possible"] = True
            fix["actions"] = fix_actions
        fix["recommendation"] = _recommendation(rule_id, rc, mode, current, expected, target)
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
    role = p["role"]["role"]
    if role in STRUCTURAL_SKIP_ROLES:
        return "skip", STRUCTURAL_SKIP_ROLES[role]
    if role == "unclassified":
        return "unknown_role", "역할 미분류: %s" % (p["role"].get("unclassified_reason") or "근거 부족")
    if p["role"]["confidence"] < doc.role_min:
        return "unknown_role", "역할 신뢰도 %.2f < 기준 %.2f" % (p["role"]["confidence"], doc.role_min)
    by_role = rc.get("by_role") or {}
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
        current["role_label"] = p["role"]["label"]
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
        # 방향은 별도 규칙에서 보므로 짧은 변/긴 변 기준으로 크기만 비교
        ok = abs(min(w, h) - rc["width_mm"]) <= tol and abs(max(w, h) - rc["height_mm"]) <= tol
        reason = "용지 짧은 변/긴 변이 %g×%gmm 와 허용오차 ±%gmm 이내로 %s" % (
            rc["width_mm"], rc["height_mm"], tol, "일치" if ok else "불일치")
        actions = None if ok else [{
            "op": "set_page_size", "section_index": sec["index"],
            "xml": "Contents/section%d.xml hp:secPr/hp:pagePr@width,@height" % sec["index"],
            "from_mm": {"width": w, "height": h}, "to_mm": {"width": rc["width_mm"], "height": rc["height_mm"]},
            "note": "HWPUNIT 환산값(1mm=7200/25.4)으로 기록. 본문 영역이 바뀌어 줄·쪽 나눔이 달라질 수 있음"}]
        out.append(make_result(doc, "PAPER_SIZE", PASS if ok else FAIL, reason, target, current=current,
                               expected=expected, fix_actions=actions))
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


def rule_page_margin(doc):
    rc = doc.rule_cfg("PAGE_MARGIN")
    out = []
    std = sorted(rc["values_mm_unordered"])
    for sec in doc.sections:
        pg = _page(doc, sec) or {}
        m = pg.get("margin_mm")
        target = {"type": "section", "section_index": sec["index"], "path": "sec%d/secPr/pagePr/margin" % sec["index"]}
        expected = {"value": rc["values_mm_unordered"], "display": "20 / 20 / 20 / 10mm (방향 미확정)", "basis": rc["spec_text"]}
        if not m:
            out.append(make_result(doc, "PAGE_MARGIN", UNKNOWN, "여백 정보 없음", target, expected=expected,
                                   unknown_reason="hp:pagePr/hp:margin 미발견", needed_info="구역 설정"))
            continue
        cur = [m.get(k) for k in ("top", "bottom", "left", "right")]
        current = {"value": {k: m.get(k) for k in ("top", "bottom", "left", "right")},
                   "display": "위 %g / 아래 %g / 왼쪽 %g / 오른쪽 %gmm" % tuple(cur), "source": VALUE_SOURCE}
        tol = doc.cfg.get("tolerance_mm", 0.5)
        same_set = all(abs(a - b) <= tol for a, b in zip(sorted(cur), std))
        obs = "값 집합이 표준과 %s" % ("일치(방향 대응은 미확정)" if same_set else "다름")
        out.append(make_result(doc, "PAGE_MARGIN", UNKNOWN, "'분석만' 항목이며 방향별 대응이 미확정이라 판정하지 않음",
                               target, current=current, expected=expected, observation=obs,
                               unknown_reason="FORMAT_SPEC: 20/20/20/10mm 의 방향별 순서 미확정",
                               needed_info="여백 값의 위/아래/왼쪽/오른쪽 대응 확정"))
    return out


def rule_header_footer_margin(doc):
    rc = doc.rule_cfg("HEADER_FOOTER_MARGIN")
    out = []
    for sec in doc.sections:
        m = (_page(doc, sec) or {}).get("margin_mm")
        target = {"type": "section", "section_index": sec["index"], "path": "sec%d/secPr/pagePr/margin" % sec["index"]}
        expected = {"value": {"header": rc["header_mm"], "footer": rc["footer_mm"]},
                    "display": "머리말 %g / 꼬리말 %gmm" % (rc["header_mm"], rc["footer_mm"]), "basis": rc["spec_text"]}
        if not m or m.get("header") is None or m.get("footer") is None:
            out.append(make_result(doc, "HEADER_FOOTER_MARGIN", UNKNOWN, "머리말·꼬리말 여백 정보 없음", target,
                                   expected=expected, unknown_reason="hp:margin@header/@footer 미발견",
                                   needed_info="구역 설정"))
            continue
        current = {"value": {"header": m["header"], "footer": m["footer"]},
                   "display": "머리말 %g / 꼬리말 %gmm" % (m["header"], m["footer"]), "source": VALUE_SOURCE}
        tol = doc.cfg.get("tolerance_mm", 0.5)
        ok = abs(m["header"] - rc["header_mm"]) <= tol and abs(m["footer"] - rc["footer_mm"]) <= tol
        if ok:
            out.append(make_result(doc, "HEADER_FOOTER_MARGIN", PASS, "머리말·꼬리말 여백이 표준값과 일치",
                                   target, current=current, expected=expected))
        else:
            out.append(make_result(doc, "HEADER_FOOTER_MARGIN", UNKNOWN,
                                   "'분석만' 항목이므로 불일치를 표준 위반으로 확정하지 않음",
                                   target, current=current, expected=expected, observation="표준값과 다름",
                                   unknown_reason="FORMAT_SPEC 처리 방식이 '분석만'",
                                   needed_info="처리 방식이 '자동 수정' 등으로 확정되면 판정 가능"))
    return out


def rule_doc_title_box(doc):
    rc = doc.rule_cfg("DOC_TITLE_BOX")
    out = []
    boxes = []
    for t in doc.tables.values():
        if t.get("context") != "body" or t.get("nesting_depth", 0) != 0 or t.get("cols") != 1 or (t.get("rows") or 0) > 3:
            continue
        titles = [i for c in t["cells"] for i in c["paragraphs"]
                  if doc.paragraphs[i]["role"]["role"] == "document_title"]
        if titles:
            boxes.append((t, titles))
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


def rule_word_break(doc):
    return [make_result(doc, "WORD_BREAK", SKIP, "어절 단위 자동 자간 조정은 현재 검사 범위 밖",
                        unknown_reason="실제 줄바꿈 위치는 렌더링 결과이며 HWPX 의 linesegarray 는 레이아웃 캐시라 신뢰하지 않음. 조정 방식도 미확정",
                        needed_info="렌더링 엔진 또는 한글 자동화로 줄바꿈 위치 확인, 조정 방식 확정")]


def rule_font_size(doc):
    st = (doc.r.get("statistics") or {}).get("by_role") or {}
    obs = {}
    for role, info in st.items():
        sizes = (info.get("fields") or {}).get("size_pt") or []
        if sizes:
            obs[role] = {str(s["value"]): s["count"] for s in sizes[:5]}
    return [make_result(doc, "FONT_SIZE", UNKNOWN, "글자 크기 표준값이 미확정",
                        current={"value": obs, "display": "역할별 글자 크기 분포(문단 수)", "source": VALUE_SOURCE},
                        unknown_reason="FORMAT_SPEC: 글자 크기 '미확정', 처리 방식 '분석만'",
                        needed_info="역할별 글자 크기 표준값")]


def rule_para_spacing(doc):
    st = (doc.r.get("statistics") or {}).get("by_paragraph") or {}
    obs = {k: {str(i["value"]): i["count"] for i in (st.get(k) or [])[:5]} for k in ("space_before_pt", "space_after_pt")}
    return [make_result(doc, "PARA_SPACING", UNKNOWN, "문단 간 간격의 산정 방식이 미확정",
                        current={"value": obs, "display": "문단 위/아래 간격 분포(pt, 문단 수)", "source": VALUE_SOURCE},
                        unknown_reason="FORMAT_SPEC: '14pt 글자 크기의 1행'의 산정 방식 미확정, 처리 방식 '분석만'",
                        needed_info="문단 간격을 문단 위/아래 여백으로 볼지, 빈 줄로 볼지 등 산정 방식")]


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
    std = doc.rule_cfg(rule_id)["value"]

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

    def compare_fn(p, current, std):
        expected = {"value": {"type": "PERCENT", "value": std}, "display": "%d%%" % std,
                    "basis": doc.rule_cfg("LINE_SPACING")["spec_text"]}
        cv = current["value"]
        if cv["type"] != "PERCENT":
            return FAIL, "줄 간격 종류가 '%s'(%s)로 표준의 비율(%%) 방식과 다름" % (
                LINE_TYPE_LABEL.get(cv["type"], cv["type"]), current["display"]), expected, None
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
            if rc.get("numeric_cell_undecided") and any(ch.isdigit() for ch in text) and NUMERIC_CELL.match(text):
                return UNKNOWN, "숫자 셀", expected, "숫자의 자리수 맞춤 방식이 미확정(FORMAT_SPEC 4)"
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


def rule_item_number(doc):
    rc = doc.rule_cfg("ITEM_NUMBER")
    results, skipped = [], []
    allowed = set(rc["allowed_kinds"])
    ex = rc["kind_examples"]
    numbered = 0
    for p in doc.text_paragraphs():
        m = p.get("list_marker")
        if not m or m.get("type") != "number":
            continue
        role = p["role"]["role"]
        if role in STRUCTURAL_SKIP_ROLES:
            skipped.append((p, STRUCTURAL_SKIP_ROLES[role]))
            continue
        if role in NUMBER_SKIP_ROLES:
            skipped.append((p, "제목에 붙은 번호는 '항목 번호' 규칙의 적용 대상인지 명세에 명시되지 않음"))
            continue
        numbered += 1
        current = {"value": m["marker"], "display": "'%s' (%s 형식)" % (m["marker"], ex.get(m["kind"], m["kind"])),
                   "source": "문단 텍스트(번호는 본문 텍스트에 포함된 문자)"}
        expected = {"value": rc["allowed_kinds"], "display": rc["spec_text"], "basis": rc["spec_text"]}
        if m["kind"] in allowed:
            results.append(make_result(doc, "ITEM_NUMBER", PASS, "번호 형식 '%s' 는 표준 번호 체계에 포함" % ex[m["kind"]],
                                       target_of(p), role_of(p), current, expected))
        else:
            results.append(make_result(doc, "ITEM_NUMBER", FAIL, "번호 형식 '%s' 는 표준 번호 체계(1. 가. 1) 가) ① ㉮)에 없음" % ex.get(m["kind"], m["kind"]),
                                       target_of(p), role_of(p), current, expected))
    if numbered:
        results.append(make_result(doc, "ITEM_NUMBER", UNKNOWN, "번호의 상하위 순서(1. → 가. → …)는 판정하지 않음",
                                   unknown_reason="항목 수준(상하위 관계)을 판단할 들여쓰기 1칸 단위가 미확정(FORMAT_SPEC 5)",
                                   needed_info="들여쓰기 1칸의 실제 단위", extra={"sub_item": "번호 순서"}))
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
# Inspector 의 기호 패턴에 없는 영문 o/O 기호를 보완 감지 (뒤에 공백이 올 때만)
LATIN_O_BULLET = re.compile(r"^([oO])\s")


def detect_bullet(p):
    m = p.get("list_marker")
    if m and m.get("type") == "bullet":
        return m["marker"]
    lm = LATIN_O_BULLET.match((p.get("text") or "").lstrip())
    return lm.group(1) if lm else None


def rule_item_bullet(doc):
    rc = doc.rule_cfg("ITEM_BULLET")
    results, skipped = [], []
    allowed = rc["allowed"]
    for p in doc.text_paragraphs():
        b = detect_bullet(p)
        if not b:
            continue
        role = p["role"]["role"]
        if role in STRUCTURAL_SKIP_ROLES:
            skipped.append((p, STRUCTURAL_SKIP_ROLES[role]))
            continue
        current = {"value": b, "display": "%s (U+%04X)" % (b, ord(b[0])), "source": "문단 텍스트(기호는 본문 텍스트에 포함된 문자)"}
        expected = {"value": allowed, "display": " → ".join(allowed), "basis": rc["spec_text"]}
        if b in BULLET_SUGGEST:
            expected["suggested"] = BULLET_SUGGEST[b]
            expected["display"] = "%s (표준 기호 %s)" % (BULLET_SUGGEST[b], " → ".join(allowed))
        if b in allowed:
            results.append(make_result(doc, "ITEM_BULLET", PASS, "기호 '%s' 는 표준 기호에 포함" % b,
                                       target_of(p), role_of(p), current, expected))
        else:
            why = BULLET_LOOKALIKES.get(b, "표준 기호(□ ㅇ - ․)에 없는 문자")
            results.append(make_result(doc, "ITEM_BULLET", FAIL, "기호 '%s': %s" % (b, why),
                                       target_of(p), role_of(p), current, expected))
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


# ----------------------------------------------------------------------------
# 레지스트리: (규칙 ID, 함수). 순서가 보고서 순서가 된다.
# ----------------------------------------------------------------------------
RULES = [
    ("DOC_TITLE_BOX", rule_doc_title_box),
    ("PAPER_SIZE", rule_paper_size),
    ("PAPER_ORIENTATION", rule_paper_orientation),
    ("PAGE_MARGIN", rule_page_margin),
    ("HEADER_FOOTER_MARGIN", rule_header_footer_margin),
    ("WORD_BREAK", rule_word_break),
    ("FONT", rule_font),
    ("FONT_SIZE", rule_font_size),
    ("CHAR_RATIO", rule_char_ratio),
    ("CHAR_SPACING", rule_char_spacing),
    ("LINE_SPACING", rule_line_spacing),
    ("PARA_ALIGN", rule_para_align),
    ("PARA_SPACING", rule_para_spacing),
    ("ITEM_NUMBER", rule_item_number),
    ("ITEM_BULLET", rule_item_bullet),
    ("DATE_FORMAT", rule_date_format),
    ("TIME_FORMAT", rule_time_format),
]

"""검사 결과(dict)를 사람이 읽는 한국어 TXT 보고서로 변환.

위치는 Inspector 문단 인덱스와 XML 경로로만 표시한다(쪽 번호는 산출하지 않음).
"""

from collections import OrderedDict, defaultdict

STATUS_LABEL = {"PASS": "적합", "FAIL": "수정 필요", "UNKNOWN": "판단 불가", "SKIP": "검사 제외"}
CONTAINER_LABEL = {"body": "본문 영역", "table_cell": "표", "textbox": "글상자", "header": "머리말",
                   "footer": "꼬리말", "footnote": "각주", "endnote": "미주", "caption": "캡션"}
MODE_LABEL = {"auto": "자동 수정", "auto_if_role_confirmed": "역할 확인 후 자동 수정",
              "manual_review": "수동 검토(자동 수정 안 함)", "none": "수정 계획 없음"}
OP_LABEL = {
    "set_char_font": "글꼴 변경", "set_char_ratio": "장평 변경", "set_char_spacing": "자간 변경",
    "set_para_line_spacing": "줄 간격 변경", "set_para_space_before": "문단 위 간격 설정",
    "set_para_hanging_indent": "내어쓰기 설정", "set_para_align": "정렬 변경", "set_page_size": "용지 크기 변경",
    "set_char_size": "글자 크기 변경", "set_page_margin": "쪽 여백 변경", "set_cell_border": "표 양단선 제거",
    "set_page_break": "대제목 쪽 나눔 설정", "delete_empty_paragraph": "빈 문단 삭제(쪽 나눔 대제목 앞)",
    "replace_marker": "항목 번호·기호 문자 교정", "add_page_number": "쪽 번호 추가",
    "set_para_word_break": "줄 나눔 기준 변경(어절 단위)",
}


def location(t):
    t = t or {}
    typ = t.get("type")
    if typ == "paragraph":
        parts = ["문단 #%d" % t["paragraph_index"]]
        if t.get("table_id"):
            c = t.get("cell") or {}
            parts.append("표 %s %d행 %d열" % (t["table_id"], (c.get("row") or 0) + 1, (c.get("col") or 0) + 1))
        elif t.get("container") and t["container"] != "body":
            parts.append(CONTAINER_LABEL.get(t["container"], t["container"]))
        return "%s (%s)" % (", ".join(parts), t.get("path"))
    if typ == "paragraph_group":
        idx = t.get("paragraph_indices") or []
        shown = ", ".join("#%d" % i for i in idx[:15]) + (" 외 %d개" % (len(idx) - 15) if len(idx) > 15 else "")
        return "문단 %d개: %s" % (len(idx), shown)
    if typ == "section":
        return "구역 %d (%s)" % (t["section_index"], t.get("path"))
    if typ == "table":
        return "표 %s (%s), 문단 #%s" % (t["table_id"], t.get("path"), t.get("paragraph_index"))
    return "문서 전체"


def _role(r):
    role = r.get("role") or {}
    if "label" in role:
        return "%s (추정, 신뢰도 %.2f)" % (role["label"], role["confidence"])
    if "roles" in role:
        return ", ".join("%s %d" % kv for kv in role["roles"].items())
    return "-"


def _disp(v):
    return (v or {}).get("display") or "-"


def _excerpt(r):
    return (r.get("target") or {}).get("text_excerpt")


def render_report(res):
    L = []
    w = L.append
    d, s = res["document"], res["summary"]
    w("=" * 90)
    w("HWPX 표준 서식 검사 보고서")
    w("=" * 90)
    w("")
    w("① 문서 검사 요약")
    w("-" * 90)
    w("  검사 문서    : %s" % d.get("file_name"))
    w("  SHA-256      : %s" % d.get("sha256"))
    w("  입력 방식    : %s" % ("HWPX 직접 입력(Inspector 내부 실행)" if d.get("input_mode") == "hwpx" else "Inspector 결과 JSON"))
    w("  원본 불변    : %s" % ("확인됨" if d.get("source_unchanged") else "확인 안 됨"))
    w("  표준 기준    : %s / 설정 %s" % (res["checker"].get("spec_version"), res["checker"].get("config_path")))
    w("  검사 문단    : 전체 %d개 중 텍스트가 있는 %d개" % (d.get("paragraph_count", 0), d.get("checked_paragraph_count", 0)))
    dt = d.get("document_type") or {}
    w("  문서 유형    : %s (%s)" % (("TYPE " + dt["value"]) if dt.get("value") else "미지정 - TYPE A/B 특례 판단 불가", dt.get("source", "미지정")))
    hs = d.get("hierarchy") or {}
    if hs:
        lv = ", ".join("%s단계 %d" % kv for kv in sorted((hs.get("items_by_level") or {}).items())) or "없음"
        um = ", ".join("'%s' %d" % kv for kv in (hs.get("unmapped_markers") or {}).items()) or "없음"
        w("  항목 계층    : %s / 일반 본문 %d / 계층 미정 번호·기호 %s" % (lv, hs.get("body", 0), um))
    w("")
    w("  검사 항목 수 : %d건" % s["total"])
    w("  적합(PASS)        : %d건" % s["PASS"])
    w("  수정 필요(FAIL)   : %d건" % s["FAIL"])
    w("  판단 불가(UNKNOWN): %d건" % s["UNKNOWN"])
    w("  검사 제외(SKIP)   : %d건" % s["SKIP"])
    w("")
    w("  %-22s %-12s %5s %5s %7s %5s" % ("검사 항목", "처리 방식", "적합", "수정", "판단불가", "제외"))
    for rid, b in s["by_rule"].items():
        w("  %-22s %-12s %5d %5d %7d %5d" % (b["name"], b["spec_mode"], b["PASS"], b["FAIL"], b["UNKNOWN"], b["SKIP"]))
    w("")
    w("  ※ 위치의 '#번호'는 Inspector 문단 인덱스(0부터, 표·머리말 포함 문서 순서)이며 쪽 번호가 아님.")
    w("  ※ '구분'의 역할은 Inspector 가 추정한 값이고, 현재값은 HWPX 에서 추출한 실제 값임.")
    w("")

    results = res["results"]
    fails = [r for r in results if r["status"] == "FAIL"]
    w("② 수정이 필요한 항목 (%d건)" % len(fails))
    w("-" * 90)
    if not fails:
        w("  없음")
    for r in fails:
        w("")
        w("[FAIL] %s%s" % (r["rule_name"], (" - " + r["sub_item"]) if r.get("sub_item") else ""))
        w("  위치    : %s" % location(r["target"]))
        if _excerpt(r):
            w("  내용    : %s" % _excerpt(r))
        w("  구분    : %s" % _role(r))
        w("  현재값  : %s" % _disp(r.get("current")))
        w("  표준값  : %s" % _disp(r.get("expected")))
        w("  판단근거: %s" % r["reason"])
        if r.get("observation"):
            w("  참고    : %s" % r["observation"])
        if r.get("interpretation_note"):
            w("  해석주의: %s" % r["interpretation_note"])
        w("  수정방법: %s" % (r["fix"].get("recommendation") or "-"))
        if r["fix"].get("blocked_reason"):
            w("  자동수정 불가 사유: %s" % r["fix"]["blocked_reason"])
        w("  수정구분: %s" % MODE_LABEL.get(r["fix"]["mode"], r["fix"]["mode"]))
    w("")

    passes = [r for r in results if r["status"] == "PASS"]
    w("③ 표준에 적합한 항목 (%d건)" % len(passes))
    w("-" * 90)
    by_rule = OrderedDict()
    for r in passes:
        # 역할별로 표준값이 다른 항목은 표준값별로 나누어 표시
        by_rule.setdefault((r["rule_name"], _disp(r.get("expected"))), []).append(r)
    if not passes:
        w("  없음")
    for (name, _), rs in by_rule.items():
        locs = []
        for r in rs:
            t = r["target"]
            locs.append("#%d" % t["paragraph_index"] if t.get("type") == "paragraph"
                        else location(t))
        shown = ", ".join(locs[:20]) + (" 외 %d건" % (len(locs) - 20) if len(locs) > 20 else "")
        w("  [PASS] %s %d건 (표준 %s): %s" % (name, len(rs), _disp(rs[0].get("expected")), shown))
    w("")

    unk = [r for r in results if r["status"] == "UNKNOWN"]
    skp = [r for r in results if r["status"] == "SKIP"]
    w("④ 판단 불가 및 검사 제외 항목 (판단 불가 %d건 / 검사 제외 %d건)" % (len(unk), len(skp)))
    w("-" * 90)
    # 같은 항목·같은 사유는 묶어서 표시
    groups = defaultdict(list)
    for r in unk:
        groups[(r["rule_name"] + ((" - " + r["sub_item"]) if r.get("sub_item") else ""),
                r.get("unknown_reason") or r["reason"], r.get("needed_info"))].append(r)
    for (name, why, need), rs in groups.items():
        w("")
        w("[UNKNOWN] %s (%d건)" % (name, len(rs)))
        w("  사유      : %s" % why)
        if need:
            w("  필요 정보 : %s" % need)
        for r in rs[:8]:
            line = "    - %s" % location(r["target"])
            if r.get("role") and "label" in r["role"]:
                line += " | %s" % r["role"]["label"]
            if r.get("current"):
                line += " | 현재 %s" % _disp(r["current"])
            if r.get("observation"):
                line += " | 관찰: %s" % r["observation"]
            w(line)
        if len(rs) > 8:
            w("    ... 외 %d건 (JSON 참조)" % (len(rs) - 8))
    for r in skp:
        w("")
        w("[SKIP] %s" % r["rule_name"])
        w("  대상 : %s" % location(r["target"]))
        if r.get("role") and r["role"].get("roles"):
            w("  역할 : %s" % _role(r))
        w("  사유 : %s" % (r.get("unknown_reason") or r["reason"]))
        if r.get("needed_info"):
            w("  필요 정보 : %s" % r["needed_info"])
    w("")

    plan = res["fix_plan"]
    w("⑤ 향후 수정 계획 (Formatter 입력)")
    w("-" * 90)
    w("  [보존 조건]")
    for x in plan["preservation_rules"]:
        w("   - %s" % x)
    for title, key in (("자동 수정 대상", "auto_actions"), ("역할 확인 후 자동 수정 대상", "role_based_actions")):
        acts = plan[key]
        w("")
        w("  [%s] %d건" % (title, len(acts)))
        for a in acts:
            ref = ("charPr %s" % a["char_pr_id"]) if "char_pr_id" in a else \
                  ("paraPr %s" % a["para_pr_id"]) if "para_pr_id" in a else \
                  ("borderFill %s" % a["border_fill_id"]) if "border_fill_id" in a else \
                  ("문단 #%s" % a["paragraph_index"]) if "paragraph_index" in a else ("구역 %s" % a.get("section_index"))
            w("   - %s | %s | %s → %s | %s%s" % (
                OP_LABEL.get(a["op"], a["op"]), ref, _short(a.get("from", a.get("from_mm"))), _short(a.get("to", a.get("to_mm"))),
                "대상 셀 %d개" % len(a["cells"]) if a.get("cells") else "대상 문단 %d개" % len(a["paragraphs"]),
                " | 다른 문단 %d개와 공유 → 정의 복제 필요" % len(a["shared_by_other_paragraphs"])
                if a.get("clone_required") else ""))
            w("     XML: %s" % a.get("xml"))
            if a["op"] == "set_char_font" and not a.get("target_font_registered"):
                w("     주의: 문서 글꼴 목록에 '%s' 가 없어 hh:fontface 에 글꼴 등록이 먼저 필요" % a["to"])
            if a.get("note"):
                w("     참고: %s" % a["note"])
    w("")
    w("  [수동 검토 대상 - 자동 수정하지 않음] %d건" % len(plan["manual_review"]))
    for m in plan["manual_review"][:40]:
        w("   - %s | %s | 현재 %s → 표준 %s%s" % (m["rule_name"], location(m["target"]), m["current"], m["expected"],
                                            (" | 사유: " + m["blocked_reason"]) if m.get("blocked_reason") else ""))
    if len(plan["manual_review"]) > 40:
        w("   ... 외 %d건 (JSON 참조)" % (len(plan["manual_review"]) - 40))
    w("")
    w("  %s" % plan["not_planned"])
    return "\n".join(L) + "\n"


def _short(v):
    if isinstance(v, dict):
        return ", ".join("%s=%s" % kv for kv in v.items())
    return str(v)

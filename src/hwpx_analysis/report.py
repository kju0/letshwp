"""분석 결과(dict)를 사람이 읽는 텍스트 보고서로 변환."""

LEVEL_LABEL = {"exact": "정확", "derived": "계산", "heuristic": "추정",
               "unverified": "의미 미확인", "unsupported": "미지원"}


def _fmt(v, suffix=""):
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "Y" if v else "N"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return "%s%s" % (v, suffix)


def _excerpt(s, n=40):
    s = (s or "").replace("\n", "⏎").replace("\t", "→").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _top(items, n=5):
    return ", ".join("%s(%d)" % (_fmt(i["value"]), i["count"]) for i in items[:n]) or "-"


def render_report(r, max_paragraphs=None):
    L = []
    w = L.append
    src, ver = r["source"], r["verification"]
    w("=" * 100)
    w("HWPX 서식 분석 보고서  -  %s" % src["file_name"])
    w("=" * 100)
    w("분석기 %s / 생성 %s" % (r["analyzer"]["version"], r["analyzer"]["generated_at"]))
    w("SHA-256 %s (%d bytes)" % (src["sha256"], src["size_bytes"]))
    app = (r["package"].get("version") or {})
    w("작성 프로그램: %s %s / OWPML xmlVersion %s" % (app.get("application"), app.get("appVersion"), app.get("xmlVersion")))
    w("")
    w("[검증]")
    w("  원본 불변: %s" % ("예" if src["unchanged_after_analysis"] else "아니오"))
    for s in ver["sections"]:
        w("  %s  문단 %d/%d  표 %d/%d  그림 %d/%d" % (
            s["part"], s["visited"]["p"], s["xml_counts"].get("p", 0), s["visited"]["tbl"],
            s["xml_counts"].get("tbl", 0), s["visited"]["pic"], s["xml_counts"].get("pic", 0)))
    tc = ver["text_coverage"]
    w("  텍스트 대조: %s (hp:t %d자 / 추출 %d자)" % ("일치" if tc["ok"] else "불일치", tc["raw_hp_t_chars"], tc["extracted_chars"]))
    w("  표 격자 무결성: %s" % ("정상" if ver["tables_grid_ok"] else "이상 %s" % ver["tables_grid_failures"]))
    w("  미처리 요소: %s" % (ver["unhandled_elements"] or "없음"))
    w("  종합: %s" % ("통과" if ver["all_ok"] else "실패"))
    w("")

    w("[페이지 설정]")
    for sec in r["document"]["sections"]:
        props = sec.get("properties") or {}
        pg = props.get("page") or {}
        m = pg.get("margin_mm") or {}
        w("  구역 %d: %s %s×%smm (치수 기준 %s, landscape 속성 원값 %s)" % (
            sec["index"], pg.get("paper") or "비표준", _fmt(pg.get("width_mm")), _fmt(pg.get("height_mm")),
            pg.get("orientation_by_dimensions"), pg.get("landscape_attr_raw")))
        w("    여백(mm) 위 %s / 아래 %s / 왼쪽 %s / 오른쪽 %s / 머리말 %s / 꼬리말 %s / 제본 %s" % tuple(
            _fmt(m.get(k)) for k in ("top", "bottom", "left", "right", "header", "footer", "gutter")))
        cols = sec.get("columns") or []
        if cols:
            w("    단: %s" % ", ".join("%s %s단" % (c["type"], c["count"]) for c in cols[:3]))
        if sec.get("page_numbers"):
            w("    쪽 번호: %s" % ", ".join("%s/%s" % (p["pos"], p["format"]) for p in sec["page_numbers"]))
        if sec.get("header_footer"):
            w("    머리말/꼬리말: %s" % ", ".join("%s(%s)" % (h["kind"], h["apply_page_type"]) for h in sec["header_footer"]))
    w("")

    cls = r["classification"]
    b = cls["baseline"]
    w("[역할 분류]  본문 기준: %s %spt%s (%s, %d개 문단)  문서 최대 %spt" % (
        b.get("body_font"), _fmt(b.get("body_size_pt")), " 진하게" if b.get("body_bold") else "",
        b.get("source"), b.get("sample_paragraphs", 0), _fmt(b.get("max_size_pt"))))
    st = r["statistics"]
    for role, info in sorted(st["by_role"].items(), key=lambda kv: -kv[1]["paragraphs"]):
        w("  %-16s %-14s %4d개  평균 신뢰도 %s" % (role, cls["role_labels"].get(role, role), info["paragraphs"],
                                              _fmt(info["avg_confidence"])))
    w("")

    w("[문단별 서식]  (빈 문단 생략)")
    hdr = "%5s %-6s %-10s %4s | %-12s %5s %4s %4s %2s | %-7s %6s %5s %5s | %s" % (
        "#", "영역", "역할", "신뢰", "글꼴", "크기", "장평", "자간", "굵", "정렬", "줄간격", "위", "아래", "텍스트")
    w(hdr)
    w("-" * 100)
    ctx_short = {"body": "본문", "table_cell": "표", "textbox": "글상자", "header": "머리말", "footer": "꼬리말",
                 "footnote": "각주", "endnote": "미주", "caption": "캡션", "group": "묶음"}
    shown = 0
    for p in r["paragraphs"]:
        if p["is_empty"]:
            continue
        if max_paragraphs is not None and shown >= max_paragraphs:
            w("  ... (이하 생략, 전체는 JSON 참조)")
            break
        shown += 1
        cf, pf, role = p.get("char_format") or {}, p.get("para_format") or {}, p["role"]
        w("%5d %-6s %-10s %4s | %-12s %5s %4s %4s %2s | %-7s %6s %5s %5s | %s%s" % (
            p["index"], ctx_short.get(p["context"]["container"], p["context"]["container"])[:6],
            role["label"][:10], _fmt(role["confidence"]),
            (cf.get("font_hangul") or "-")[:12], _fmt(cf.get("size_pt")), _fmt(cf.get("ratio_hangul")),
            _fmt(cf.get("spacing_hangul")), "B" if cf.get("bold") else "",
            _fmt(pf.get("align"))[:7], _fmt(pf.get("line_spacing")), _fmt(pf.get("space_before_pt")),
            _fmt(pf.get("space_after_pt")), "*" if p.get("char_format_mixed") else "", _excerpt(p["text"])))
    w("  (* = 문단 안에 글자 모양이 2종 이상 혼재. 표시값은 글자 수 기준 대표값)")
    w("")

    w("[미분류 문단과 사유]")
    un = [p for p in r["paragraphs"] if p["role"]["role"] == "unclassified"]
    if not un:
        w("  없음")
    for p in un[:60]:
        cands = ", ".join("%s %.2f" % (c["label"], c["score"]) for c in p["role"]["candidates"][:3])
        w("  #%d %s  사유: %s  후보: %s" % (p["index"], _excerpt(p["text"], 30), p["role"]["unclassified_reason"], cands or "-"))
    if len(un) > 60:
        w("  ... 외 %d개" % (len(un) - 60))
    w("")

    w("[분류 근거 예시]  (역할별 첫 문단)")
    seen = set()
    for p in r["paragraphs"]:
        role = p["role"]["role"]
        if role in seen or role in ("empty",):
            continue
        seen.add(role)
        w("  #%d [%s %.2f] %s" % (p["index"], p["role"]["label"], p["role"]["confidence"], _excerpt(p["text"], 40)))
        for e in p["role"]["evidence"][:6]:
            w("      %+.2f %-10s %s" % (e["weight"], e["role"], e["detail"]))
    w("")

    w("[표]  %d개" % len(r["tables"]))
    for t in r["tables"]:
        g = t["grid_check"]
        s = t.get("text_format_summary") or {}
        f = s.get("all_cells_formats") or {}
        w("  %s (%s, 깊이 %d) %s×%s 셀 %s개 병합 %s개  크기 %s×%smm  위치: 문단 #%d" % (
            t["id"], t["context"], t["nesting_depth"], t["rows"], t["cols"], g.get("cell_count"), g.get("merged_cells"),
            _fmt((t.get("size") or {}).get("width_mm")), _fmt((t.get("size") or {}).get("height_mm")), t["anchor_paragraph"]))
        if f:
            w("     글꼴 %s | 크기 %s | 정렬 %s | 줄간격 %s | 장평 %s | 자간 %s" % (
                _top(f["font_hangul"], 3), _top(f["size_pt"], 3), _top(f["align"], 3),
                _top(f["line_spacing"], 3), _top(f["ratio"], 2), _top(f["spacing"], 3)))
        w("     셀 세로정렬 %s | 셀 배경 %s" % (_top(s.get("cell_vert_align") or [], 3), _top(s.get("cell_fill_colors") or [], 3)))
    w("")

    w("[그림]  %d개" % len(r["pictures"]))
    for g in r["pictures"]:
        cs = g.get("current_size") or {}
        w("  %s %s %s×%smm 글자처럼취급=%s 위치: 문단 #%d (%s)" % (
            g["id"], g.get("binary_path"), _fmt(cs.get("width_mm")), _fmt(cs.get("height_mm")),
            _fmt((g.get("position") or {}).get("treat_as_char")), g["anchor_paragraph"], g["context"]))
    if r["shapes"]:
        w("[도형]  %d개 (%s)" % (len(r["shapes"]), ", ".join(sorted({s["kind"] for s in r["shapes"]}))))
    if r["other_objects"]:
        w("[기타 개체 - 미지원]  %s" % ", ".join("%s:%s" % (o["id"], o["kind"]) for o in r["other_objects"]))
    w("")

    w("[서식 빈도 - 글자 수 기준]")
    for k, label in (("font_hangul", "글꼴"), ("size_pt", "크기"), ("ratio", "장평"), ("spacing", "자간")):
        w("  %-4s %s" % (label, _top(st["by_chars"][k], 8)))
    w("[서식 빈도 - 문단 수 기준]")
    for k, label in (("align", "정렬"), ("line_spacing", "줄간격"), ("space_before_pt", "문단위"),
                     ("space_after_pt", "문단아래"), ("indent_pt", "들여쓰기")):
        w("  %-6s %s" % (label, _top(st["by_paragraph"][k], 8)))
    w("[서식 조합(시그니처)]  %d종 - 상위 10" % st["format_signature_count"])
    for s in st["format_signatures"][:10]:
        f = s["format"]
        w("  %3d문단 %5d자  %s %spt 장평%s 자간%s %s %s 줄%s 위%s 아래%s  역할 %s" % (
            s["paragraphs"], s["chars"], f["font_hangul"], _fmt(f["size_pt"]), _fmt(f["ratio"]), _fmt(f["spacing"]),
            "B" if f["bold"] else "-", f["align"], f["line_spacing"], _fmt(f["space_before_pt"]),
            _fmt(f["space_after_pt"]), s["roles"]))
    w("")

    w("[추출 항목별 신뢰 수준]")
    for s in r["support_matrix"]:
        w("  [%s] %s%s" % (LEVEL_LABEL.get(s["level"], s["level"]), s["item"], (" - " + s["note"]) if s["note"] else ""))
    w("")
    w("[경고]")
    for x in r["warnings"] or ["없음"]:
        w("  " + x)
    return "\n".join(L) + "\n"

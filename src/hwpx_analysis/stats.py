"""문서 전체 서식 종류·빈도 집계.

빈도는 두 가지로 센다.
- paragraphs : 해당 값을 대표 서식으로 가진 문단 수 (빈 문단 제외)
- chars      : 공백 제외 글자 수 (run 단위로 실제 글자 모양을 따라 집계)

영역(scope)별로 분리한다: body(본문), table_cell(표 내부), textbox(글상자),
header_footer(머리말·꼬리말), note_area(각주·미주), caption(캡션 요소).
"""

from collections import Counter, defaultdict

SCOPE_OF = {
    "body": "body", "table_cell": "table_cell", "textbox": "textbox",
    "header": "header_footer", "footer": "header_footer",
    "footnote": "note_area", "endnote": "note_area", "caption": "caption",
}

PARA_FIELDS = {
    "font_hangul": ("char_format", "font_hangul"),
    "size_pt": ("char_format", "size_pt"),
    "ratio": ("char_format", "ratio_hangul"),
    "spacing": ("char_format", "spacing_hangul"),
    "bold": ("char_format", "bold"),
    "align": ("para_format", "align"),
    "line_spacing": ("para_format", "line_spacing"),
    "space_before_pt": ("para_format", "space_before_pt"),
    "space_after_pt": ("para_format", "space_after_pt"),
    "indent_pt": ("para_format", "indent_pt"),
    "left_margin_pt": ("para_format", "left_margin_pt"),
}


def _counter_to_list(c, total=None):
    out = []
    for k, v in c.most_common():
        item = {"value": k, "count": v}
        if total:
            item["ratio"] = round(v / total, 4)
        out.append(item)
    return out


def _get(p, path):
    sec, key = path
    return (p.get(sec) or {}).get(key)


def _signature(p):
    cf, pf = p.get("char_format") or {}, p.get("para_format") or {}
    return (cf.get("font_hangul"), cf.get("size_pt"), cf.get("ratio_hangul"), cf.get("spacing_hangul"),
            bool(cf.get("bold")), pf.get("align"), pf.get("line_spacing"),
            pf.get("space_before_pt"), pf.get("space_after_pt"))


SIG_FIELDS = ("font_hangul", "size_pt", "ratio", "spacing", "bold", "align", "line_spacing",
              "space_before_pt", "space_after_pt")


def aggregate(paragraphs, header):
    nonempty = [p for p in paragraphs if not p["is_empty"]]

    def field_stats(ps):
        out = {}
        for name, path in PARA_FIELDS.items():
            c = Counter(_get(p, path) for p in ps)
            out[name] = _counter_to_list(c, len(ps))
        return out

    # run 단위 글자 수 집계(문단 대표값이 아닌 실제 글자 기준)
    char_font, char_size, char_ratio, char_spacing = Counter(), Counter(), Counter(), Counter()
    charpr_usage, parapr_usage, style_usage = Counter(), Counter(), Counter()
    for p in paragraphs:
        parapr_usage[(p.get("para_format") or {}).get("para_pr_id")] += 1
        style_usage[(p.get("style") or {}).get("name")] += 1
        for r in p.get("runs") or []:
            cp = header.char_props.get(r["char_pr_id"])
            charpr_usage[r["char_pr_id"]] += 1
            if not cp or not r["chars"]:
                continue
            n = r["chars"]
            char_font[(cp["font_face"] or {}).get("hangul")] += n
            char_size[cp["size_pt"]] += n
            char_ratio[(cp["ratio"] or {}).get("hangul")] += n
            char_spacing[(cp["spacing"] or {}).get("hangul")] += n
    total_chars = sum(char_size.values())

    by_scope = defaultdict(list)
    for p in nonempty:
        by_scope[SCOPE_OF.get(p["context"]["container"], "other:" + p["context"]["container"])].append(p)

    by_role = defaultdict(list)
    for p in paragraphs:
        by_role[p["role"]["role"]].append(p)

    sigs = defaultdict(list)
    for p in nonempty:
        sigs[_signature(p)].append(p)
    signatures = []
    for sig, ps in sorted(sigs.items(), key=lambda kv: -len(kv[1])):
        signatures.append({
            "format": dict(zip(SIG_FIELDS, sig)),
            "paragraphs": len(ps),
            "chars": sum(p["visible_length"] for p in ps),
            "roles": dict(Counter(p["role"]["role"] for p in ps)),
            "scopes": dict(Counter(SCOPE_OF.get(p["context"]["container"], p["context"]["container"]) for p in ps)),
            "examples": [p["index"] for p in ps[:5]],
        })

    role_formats = {}
    for role, ps in by_role.items():
        ps_ne = [p for p in ps if not p["is_empty"]]
        role_formats[role] = {
            "paragraphs": len(ps),
            "avg_confidence": round(sum(p["role"]["confidence"] for p in ps) / len(ps), 3) if ps else None,
            "fields": field_stats(ps_ne) if ps_ne else {},
        }

    used_char = {k for k in charpr_usage if k is not None}
    used_para = {k for k in parapr_usage if k is not None}
    return {
        "paragraph_count": len(paragraphs),
        "nonempty_paragraph_count": len(nonempty),
        "total_visible_chars_in_runs": total_chars,
        "by_chars": {
            "font_hangul": _counter_to_list(char_font, total_chars),
            "size_pt": _counter_to_list(char_size, total_chars),
            "ratio": _counter_to_list(char_ratio, total_chars),
            "spacing": _counter_to_list(char_spacing, total_chars),
        },
        "by_paragraph": field_stats(nonempty),
        "by_scope": {s: {"paragraphs": len(ps), "fields": field_stats(ps)} for s, ps in by_scope.items()},
        "by_role": role_formats,
        "role_counts": {r: len(ps) for r, ps in sorted(by_role.items(), key=lambda kv: -len(kv[1]))},
        "format_signatures": signatures,
        "format_signature_count": len(signatures),
        "definition_usage": {
            "char_pr_used": len(used_char), "char_pr_defined": len(header.char_props),
            "char_pr_unused_in_runs_ids": sorted(set(header.char_props) - used_char),
            "para_pr_used": len(used_para), "para_pr_defined": len(header.para_props),
            "style_usage": _counter_to_list(style_usage),
        },
    }


def table_text_summary(table, paragraphs):
    """표 내부(이 표의 셀에 직접 속한 문단) 글자·문단 서식 요약. 중첩 표의 문단은 해당 표에서 따로 집계."""
    idxs = [i for c in table["cells"] for i in c["paragraphs"]]
    ps = [paragraphs[i] for i in idxs if not paragraphs[i]["is_empty"]]
    header_ps = [paragraphs[i] for c in table["cells"] if c["is_header"] or c["row"] == 0
                 for i in c["paragraphs"] if not paragraphs[i]["is_empty"]]

    def fs(group):
        return {name: _counter_to_list(Counter(_get(p, path) for p in group), len(group))
                for name, path in PARA_FIELDS.items()} if group else {}

    return {
        "nonempty_paragraphs": len(ps),
        "all_cells_formats": fs(ps),
        "first_row_or_header_cells_formats": fs(header_ps),
        "cell_vert_align": _counter_to_list(Counter(c["vert_align"] for c in table["cells"])),
        "cell_fill_colors": _counter_to_list(Counter((c.get("fill") or {}).get("face_color") for c in table["cells"])),
        "mixed_char_format_paragraphs": sum(1 for p in ps if p.get("char_format_mixed")),
    }

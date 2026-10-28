"""항목 계층 인식: 항목 번호(1~6단계)·항목 기호(□ ㅇ - ․ 층위)·참고·주석·일반 본문의 관계 (FORMAT_SPEC 5장).

판단 대상은 본문 영역의 최상위 문단(context.container == 'body', depth 0)이다.
표 셀·머리말·각주·글상자 문단은 범위 밖(out_of_scope)으로 표시한다.

문단 종류(kind)
  item           1~6단계로 대응되는 번호(본문 텍스트 또는 자동 번호)가 있는 항목. level = 1~6
  bullet         표준 항목 기호(□ ㅇ - ․)로 시작하는 항목. level = 기호 층위(1~4)
  unmapped_item  번호·기호가 있으나 표준 대응이 없는 항목(Ⅰ., (1), ■, ○ 등)
  note           '*' 또는 '※'로 시작하는 참고·주석 문장. 층위 판단에서 제외된다(요구사항 5)
  body           번호·기호가 없고 Inspector 역할이 '본문'인 일반 본문
  other          제목/캡션/미분류 등 계층 규칙으로 소속을 정할 수 없는 문단
  object         텍스트 없이 표·그림만 있는 문단
  empty          빈 문단

관계 기록
- prev_marked: 직전의 번호·기호 항목(item/bullet). 일반 본문·주석·빈 문단은 건너뛴다. 기호 항목의 간격 판단에 쓴다.
- owner_item : 일반 본문이 소속된 직전 항목(item/bullet).
- other / object / unmapped_item 이 끼어들면 소속 관계가 모호해지므로 이후 비교 대상을 비운다(chain_broken_by).
  다음 항목이 나타나면 체인은 다시 시작된다.
"""

import re

# Inspector classifier 의 list_marker.kind -> 계층
MARKER_KIND_LEVEL = {
    "digit_dot": 1, "hangul_dot": 2, "digit_paren": 3,
    "hangul_paren": 4, "circled_digit": 5, "circled_hangul": 6,
}
LEVEL_EXAMPLE = {1: "1.", 2: "가.", 3: "1)", 4: "가)", 5: "①", 6: "㉮"}
DEFAULT_BULLET_ORDER = ("□", "ㅇ", "-", "․")
DEFAULT_NOTE_MARKERS = ("*", "※")
# Inspector 기호 패턴에 없는 영문 o/O 기호(뒤에 공백) 보완 감지
LATIN_O_BULLET = re.compile(r"^([oO])\s")


def detect_bullet(p):
    m = p.get("list_marker")
    if m and m.get("type") == "bullet":
        return m["marker"]
    lm = LATIN_O_BULLET.match((p.get("text") or "").lstrip())
    return lm.group(1) if lm else None


def note_marker_of(text, note_markers=DEFAULT_NOTE_MARKERS):
    """문단이 참고·주석 기호('*', '※')로 시작하면 그 기호, 아니면 None."""
    s = (text or "").lstrip()
    for m in note_markers:
        if s.startswith(m):
            return m
    return None


def auto_number_level(an):
    """자동 번호(hh:numbering paraHead) 형식을 계층으로 대응. 대응 불가면 None."""
    if not an or an.get("type") not in ("OUTLINE", "NUMBER"):
        return None
    fmt, text = an.get("num_format"), (an.get("format_text") or "").strip()
    dot = bool(re.fullmatch(r"\^\d+\.", text))
    paren = bool(re.fullmatch(r"\^\d+\)", text))
    if fmt == "DIGIT" and dot:
        return 1
    if fmt == "HANGUL_SYLLABLE" and dot:
        return 2
    if fmt == "DIGIT" and paren:
        return 3
    if fmt == "HANGUL_SYLLABLE" and paren:
        return 4
    if fmt == "CIRCLED_DIGIT":
        return 5
    if fmt == "CIRCLED_HANGUL_SYLLABLE":
        return 6
    return None


def classify_item(p, bullet_order=DEFAULT_BULLET_ORDER, note_markers=DEFAULT_NOTE_MARKERS):
    """(kind, level, marker, level_source)"""
    ctx = p.get("context") or {}
    if ctx.get("container") != "body" or ctx.get("depth", 0) != 0:
        return "out_of_scope", None, None, None
    if p.get("is_empty"):
        return ("object" if p.get("objects") else "empty"), None, None, None
    if (p.get("role") or {}).get("role") == "toc":
        # 차례 항목은 번호가 있어도 본문 계층 항목이 아니다
        return "other", None, None, None
    nm = note_marker_of(p.get("text"), note_markers)
    if nm:
        return "note", None, nm, "참고·주석 기호"
    an = p.get("auto_numbering")
    lvl = auto_number_level(an)
    if lvl:
        return "item", lvl, (an.get("format_text") or "").strip(), "자동 번호(%s)" % an.get("num_format")
    if an and an.get("type") in ("OUTLINE", "NUMBER", "BULLET"):
        return "unmapped_item", None, an.get("format_text") or an.get("bullet_char"), "자동 번호/글머리표(대응 없음)"
    m = p.get("list_marker")
    if m and m.get("type") == "number":
        lvl = MARKER_KIND_LEVEL.get(m["kind"])
        if lvl:
            return "item", lvl, m["marker"], "본문 텍스트 번호"
        return "unmapped_item", None, m["marker"], "본문 텍스트 번호(대응 없음)"
    b = detect_bullet(p)
    if b:
        if b in bullet_order:
            return "bullet", list(bullet_order).index(b) + 1, b, "항목 기호 층위(%s)" % " → ".join(bullet_order)
        return "unmapped_item", None, b, "본문 텍스트 기호(표준 기호 아님)"
    if (p.get("role") or {}).get("role") == "body":
        return "body", None, None, None
    return "other", None, None, None


def annotate_hierarchy(paragraphs, bullet_order=None, note_markers=None):
    """각 문단에 p['hierarchy'] 를 기록하고 요약을 반환한다."""
    bullet_order = tuple(bullet_order or DEFAULT_BULLET_ORDER)
    note_markers = tuple(note_markers or DEFAULT_NOTE_MARKERS)
    last_marked = None        # 직전 항목 {"paragraph", "kind", "level", "marker"}
    chain_broken_by = None    # 소속을 모호하게 만든 문단 index
    prev_sig = None           # 직전 유의미 문단 index(빈 문단 제외)
    prev_struct = None        # 직전 구조 문단 index(빈 문단·주석 제외) - 층위 판단용
    blanks = 0
    summary = {"items_by_level": {}, "bullets_by_marker": {}, "unmapped_markers": {}, "body": 0, "note": 0, "other": 0}

    for p in paragraphs:
        kind, level, marker, src = classify_item(p, bullet_order, note_markers)
        h = {"kind": kind, "level": level, "marker": marker, "level_source": src}
        p["hierarchy"] = h
        if kind == "out_of_scope":
            continue
        if kind == "empty":
            blanks += 1
            continue
        h["previous_paragraph"] = prev_sig
        h["previous_kind"] = paragraphs[prev_sig]["hierarchy"]["kind"] if prev_sig is not None else None
        h["previous_structural_paragraph"] = prev_struct
        h["previous_structural_kind"] = paragraphs[prev_struct]["hierarchy"]["kind"] if prev_struct is not None else None
        h["blank_paragraphs_before"] = blanks
        if kind in ("item", "bullet"):
            if kind == "item":
                summary["items_by_level"][str(level)] = summary["items_by_level"].get(str(level), 0) + 1
            else:
                summary["bullets_by_marker"][marker] = summary["bullets_by_marker"].get(marker, 0) + 1
            if last_marked and chain_broken_by is None:
                h["prev_marked"] = dict(last_marked)
            else:
                h["prev_marked"] = None
                h["prev_marked_unavailable_reason"] = (
                    "앞선 항목 없음(문서 첫 항목)" if last_marked is None
                    else "직전 항목과의 사이에 소속을 모호하게 하는 문단(#%d)이 있음" % chain_broken_by)
            last_marked = {"paragraph": p["index"], "kind": kind, "level": level, "marker": marker}
            chain_broken_by = None
        elif kind == "body":
            summary["body"] += 1
            if last_marked and chain_broken_by is None:
                h["owner_item"] = dict(last_marked)
            else:
                h["owner_item"] = None
                h["owner_unavailable_reason"] = (
                    "앞선 항목 없음" if last_marked is None
                    else "소속을 모호하게 하는 문단(#%d) 이후의 본문" % chain_broken_by)
        elif kind == "note":
            summary["note"] += 1
        else:
            if kind == "unmapped_item":
                summary["unmapped_markers"][marker] = summary["unmapped_markers"].get(marker, 0) + 1
            else:
                summary["other"] += 1
            chain_broken_by = p["index"]
        prev_sig = p["index"]
        if kind != "note":
            prev_struct = p["index"]
        blanks = 0
    return summary


def item_above_spacing(level, cfg):
    """항목 번호 문단 위 간격(pt): 1단계 20, 2단계 16, 그 외 14 (요구사항 7)."""
    table = cfg["item_above_pt"]
    return table.get(str(level), table.get("default")), "%d단계 항목 위" % level


def bullet_above_spacing(prev, cur_level, cfg, after_heading_1=False):
    """항목 기호 문단 위 간격(pt). 반환 (pt, 허용값 목록, 근거) 또는 (None, (), 미정 사유).
    prev: 직전 항목 {"kind", "level", "marker"} (일반 본문·주석은 건너뛴 값).
    2026-10-28 사용자 결정: □ 는 앞 문단과 관계없이 14pt(대제목 바로 다음은 15pt 도 허용), ㅇ→ㅇ 6pt,
    한 층위 아래(□→ㅇ, ㅇ→-, -→․) 6pt."""
    order = cfg.get("bullet_order") or list(DEFAULT_BULLET_ORDER)
    cur = order[cur_level - 1]
    if cur_level == 1:
        allowed = tuple(cfg.get("bullet_square_after_heading_1_allowed_pt") or ()) if after_heading_1 else ()
        why = "%s 항목 위" % cur + (" (대제목 바로 다음이라 %s 허용)" % "·".join("%gpt" % a for a in allowed) if allowed else "")
        return cfg["bullet_square_pt"], allowed, why
    if prev is None or prev["kind"] != "bullet":
        return None, (), ("앞선 항목 없음" if prev is None
                          else "번호 항목(%s) 다음에 오는 기호 항목(%s)의 간격은 미정" % (prev.get("marker"), cur))
    if prev["level"] == cur_level - 1:
        return cfg["bullet_one_level_down_pt"], (), "%s → %s (한 층위 아래)" % (prev["marker"], cur)
    same = cfg.get("bullet_same_level_pt") or {}
    if prev["level"] == cur_level and cur in same:
        return same[cur], (), "%s → %s (같은 기호 반복)" % (cur, cur)
    return None, (), "%s → %s 간격은 미정(%s)" % (prev["marker"], cur, cfg.get("bullet_undecided") or "미정")

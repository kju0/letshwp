"""항목 계층(1~6단계) 인식과 일반 본문의 소속 판단 (FORMAT_SPEC 5.1, 5.4, 5.8).

계층 표기 기준(FORMAT_SPEC 5.1):
  1단계 `1.`  2단계 `가.`  3단계 `1)`  4단계 `가)`  5단계 `①`  6단계 `㉮`

판단 대상은 본문 영역의 최상위 문단(context.container == 'body', depth 0)이다.
표 셀·머리말·각주·글상자 문단은 범위 밖(out_of_scope)으로 표시한다.

문단 종류(kind)
  item           1~6단계로 대응되는 번호(본문 텍스트 또는 자동 번호)가 있는 항목
  unmapped_item  번호·기호가 있으나 1~6단계 대응이 명세에 없는 항목(Ⅰ., (1), □, ㅇ 등)
  body           번호·기호가 없고 Inspector 역할이 '본문'인 일반 본문
  other          제목/주석/캡션/미분류 등 계층 규칙으로 소속을 정할 수 없는 문단
  object         텍스트 없이 표·그림만 있는 문단
  empty          빈 문단

소속·비교 규칙
- 일반 본문은 직전의 가장 가까운 유효 항목(owner_item)에 소속된다.
- 새 항목은 직전 유효 항목(compare_with)과 계층을 비교한다. 직전 문단이 일반 본문이면 그 소속 항목이 된다.
- other / object / unmapped_item 이 끼어들면 소속 관계가 모호해지므로(5.4) 이후 비교 대상은 비운다(chain_broken_by).
  다음 item 이 나타나면 체인은 다시 시작된다.
"""

import re

# Inspector classifier 의 list_marker.kind -> 계층
MARKER_KIND_LEVEL = {
    "digit_dot": 1, "hangul_dot": 2, "digit_paren": 3,
    "hangul_paren": 4, "circled_digit": 5, "circled_hangul": 6,
}
LEVEL_EXAMPLE = {1: "1.", 2: "가.", 3: "1)", 4: "가)", 5: "①", 6: "㉮"}
# Inspector 기호 패턴에 없는 영문 o/O 기호(뒤에 공백) 보완 감지
LATIN_O_BULLET = re.compile(r"^([oO])\s")


def detect_bullet(p):
    m = p.get("list_marker")
    if m and m.get("type") == "bullet":
        return m["marker"]
    lm = LATIN_O_BULLET.match((p.get("text") or "").lstrip())
    return lm.group(1) if lm else None


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


def classify_item(p, bullet_levels=None):
    """(kind, level, marker, level_source)"""
    ctx = p.get("context") or {}
    if ctx.get("container") != "body" or ctx.get("depth", 0) != 0:
        return "out_of_scope", None, None, None
    if p.get("is_empty"):
        return ("object" if p.get("objects") else "empty"), None, None, None
    if (p.get("role") or {}).get("role") == "toc":
        # 차례 항목은 번호가 있어도 본문 계층 항목이 아니다
        return "other", None, None, None
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
        lvl = (bullet_levels or {}).get(b)
        if lvl:
            return "item", int(lvl), b, "본문 텍스트 기호(설정의 bullet_levels)"
        return "unmapped_item", None, b, "본문 텍스트 기호(대응 없음)"
    if (p.get("role") or {}).get("role") == "body":
        return "body", None, None, None
    return "other", None, None, None


def annotate_hierarchy(paragraphs, bullet_levels=None):
    """각 문단에 p['hierarchy'] 를 기록하고 요약을 반환한다."""
    last_item = None          # (index, level)
    stack = []                # 상위 항목 추적용 [(index, level)]
    chain_broken_by = None    # 소속을 모호하게 만든 문단 index
    prev_sig = None           # 직전 유의미 문단 index(빈 문단 제외)
    blanks = 0
    summary = {"items_by_level": {}, "unmapped_markers": {}, "body": 0, "other": 0}

    for p in paragraphs:
        kind, level, marker, src = classify_item(p, bullet_levels)
        h = {"kind": kind, "level": level, "marker": marker, "level_source": src}
        p["hierarchy"] = h
        if kind == "out_of_scope":
            continue
        if kind == "empty":
            blanks += 1
            continue
        h["previous_paragraph"] = prev_sig
        h["previous_kind"] = paragraphs[prev_sig]["hierarchy"]["kind"] if prev_sig is not None else None
        h["blank_paragraphs_before"] = blanks
        if kind == "item":
            summary["items_by_level"][str(level)] = summary["items_by_level"].get(str(level), 0) + 1
            if last_item and chain_broken_by is None:
                h["compare_with"] = {"paragraph": last_item[0], "level": last_item[1]}
            else:
                h["compare_with"] = None
                h["compare_unavailable_reason"] = (
                    "앞선 유효 항목 없음(문서 첫 항목)" if last_item is None
                    else "직전 항목과의 사이에 소속을 모호하게 하는 문단(#%d)이 있음" % chain_broken_by)
            while stack and stack[-1][1] >= level:
                stack.pop()
            h["parent_item"] = stack[-1][0] if stack else None
            stack.append((p["index"], level))
            last_item = (p["index"], level)
            chain_broken_by = None
        elif kind == "body":
            summary["body"] += 1
            if last_item and chain_broken_by is None:
                h["owner_item"] = {"paragraph": last_item[0], "level": last_item[1]}
            else:
                h["owner_item"] = None
                h["owner_unavailable_reason"] = (
                    "앞선 항목 없음" if last_item is None
                    else "소속을 모호하게 하는 문단(#%d) 이후의 본문" % chain_broken_by)
        else:
            if kind == "unmapped_item":
                summary["unmapped_markers"][marker] = summary["unmapped_markers"].get(marker, 0) + 1
            else:
                summary["other"] += 1
            chain_broken_by = p["index"]
        prev_sig = p["index"]
        blanks = 0
    return summary


def level_spacing(a, b, cfg):
    """두 항목 계층 간 간격(pt). 상위 계층(작은 숫자) 기준, 2↔3 은 14pt 예외(5.2)."""
    lo, hi = min(a, b), max(a, b)
    if {a, b} == {2, 3}:
        return cfg["level_2_3_exception_pt"], "2단계↔3단계 예외"
    base = cfg["level_base_pt"]
    v = base.get(str(lo), base.get("default"))
    return v, "상위 계층 %d단계 기준" % lo

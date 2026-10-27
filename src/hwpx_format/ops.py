"""서식 수정 동작(op) 레지스트리.

Checker 의 fix_plan 에 나오는 op 이름별로 독립 함수 하나가 대응한다.
- 정의(charPr/paraPr) 대상 op: 함수(조각 바이트, 조각 루트 Elem, action) -> [(start, end, bytes)] 편집 목록
- 구역(secPr) 대상 op: 함수(구역 바이트, pagePr Elem, action) -> 편집 목록
op 를 추가·제거하려면 이 파일의 함수와 DEF_OPS / SECTION_OPS 항목만 고치면 된다.
적용할 수 없으면 OpNotApplicable(사유) 를 던지고, 그 op 는 적용하지 않은 채 보고된다.
"""

from .xmlpatch import set_attr_edit


class OpNotApplicable(RuntimeError):
    pass


HWPUNIT_PER_PT = 100          # 1pt = 100 HWPUNIT (글자 크기 1000 = 10pt 와 같은 단위)
HWPUNIT_PER_MM = 7200 / 25.4  # 1inch = 7200 HWPUNIT


def _only(elems, what):
    if not elems:
        raise OpNotApplicable("%s 요소가 없음" % what)
    return elems


def _lang_edits(data, root, local, langs, value):
    edits = []
    for el in _only(list(root.iter(local)), "hh:" + local):
        for lang in langs:
            edits.append(set_attr_edit(data, el, lang, value))
    return edits


# --- 글자 모양(charPr) --------------------------------------------------------
def op_set_char_spacing(data, root, a):
    return _lang_edits(data, root, "spacing", a["langs"], int(a["to"]))


def op_set_char_ratio(data, root, a):
    return _lang_edits(data, root, "ratio", a["langs"], int(a["to"]))


def op_set_char_font(data, root, a):
    if not a.get("target_font_registered") or a.get("target_font_id") is None:
        raise OpNotApplicable("문서에 '%s' 글꼴이 등록되어 있지 않음(글꼴 목록 추가는 지원하지 않음)" % a["to"])
    return _lang_edits(data, root, "fontRef", [a["lang"]], int(a["target_font_id"]))


# --- 문단 모양(paraPr) --------------------------------------------------------
def op_set_para_align(data, root, a):
    return [set_attr_edit(data, el, "horizontal", a["to"]) for el in _only(list(root.iter("align")), "hh:align")]


def op_set_para_line_spacing(data, root, a):
    # hp:switch 의 case/default 양쪽 lineSpacing 이 같은 값으로 기록되는 구조(샘플 259개 전수 동일)
    to = a["to"]
    edits = []
    for el in _only(list(root.iter("lineSpacing")), "hh:lineSpacing"):
        edits.append(set_attr_edit(data, el, "type", to["type"]))
        edits.append(set_attr_edit(data, el, "value", int(to["value"])))
    return edits


def op_set_para_space_before(data, root, a):
    # case(HwpUnitChar) 와 switch 없는 margin 은 pt×100, default 는 그 2배(샘플 183개 전수 2배 관측)
    base = int(round(float(a["to"]) * HWPUNIT_PER_PT))
    edits = []
    for margin in _only(list(root.iter("margin")), "hh:margin"):
        for prev in _only(margin.kids("prev"), "hc:prev"):
            unit = prev.attrs.get("unit", "HWPUNIT")
            if unit != "HWPUNIT":
                raise OpNotApplicable("문단 위 간격 단위가 HWPUNIT 이 아님(%s)" % unit)
            value = base * 2 if margin.has_ancestor("default", stop=root) else base
            edits.append(set_attr_edit(data, prev, "value", value))
    return edits


def op_set_para_hanging_indent(data, root, a):
    # hc:intent 음수 = 내어쓰기. case(HwpUnitChar)·switch 없는 margin 은 pt×100, default 는 그 2배(샘플 관측).
    # 왼쪽 여백(hc:left)은 바꾸지 않는다.
    base = int(round(float(a["to"]) * HWPUNIT_PER_PT))
    edits = []
    for margin in _only(list(root.iter("margin")), "hh:margin"):
        for intent in _only(margin.kids("intent"), "hc:intent"):
            unit = intent.attrs.get("unit", "HWPUNIT")
            if unit != "HWPUNIT":
                raise OpNotApplicable("들여쓰기 단위가 HWPUNIT 이 아님(%s)" % unit)
            value = base * 2 if margin.has_ancestor("default", stop=root) else base
            edits.append(set_attr_edit(data, intent, "value", value))
    return edits


DEF_OPS = {
    "set_char_spacing": ("charPr", op_set_char_spacing),
    "set_char_ratio": ("charPr", op_set_char_ratio),
    "set_char_font": ("charPr", op_set_char_font),
    "set_para_align": ("paraPr", op_set_para_align),
    "set_para_line_spacing": ("paraPr", op_set_para_line_spacing),
    "set_para_space_before": ("paraPr", op_set_para_space_before),
    "set_para_hanging_indent": ("paraPr", op_set_para_hanging_indent),
}


# --- 구역(secPr) --------------------------------------------------------------
# 한글이 저장한 A4 치수(샘플 전수 관측). mm 환산 반올림값(84189)과 1 HWPUNIT 다르므로 관측값을 쓴다.
KNOWN_PAPER_HWPUNIT = {(210.0, 297.0): (59528, 84188)}


def op_set_page_size(data, page_pr, a):
    # 크기만 바꾸고 방향(가로/세로)은 바꾸지 않는다: 현재 가로가 더 길면 가로·세로를 바꿔 기록
    to = a["to_mm"]
    short_mm, long_mm = sorted((float(to["width"]), float(to["height"])))
    short_u, long_u = KNOWN_PAPER_HWPUNIT.get(
        (short_mm, long_mm), (int(round(short_mm * HWPUNIT_PER_MM)), int(round(long_mm * HWPUNIT_PER_MM))))
    try:
        landscape_now = int(page_pr.attrs["width"]) > int(page_pr.attrs["height"])
    except (KeyError, ValueError):
        raise OpNotApplicable("hp:pagePr 의 width/height 를 읽을 수 없음")
    w, h = (long_u, short_u) if landscape_now else (short_u, long_u)
    return [set_attr_edit(data, page_pr, "width", w), set_attr_edit(data, page_pr, "height", h)]


SECTION_OPS = {
    "set_page_size": op_set_page_size,
}


def supported(op):
    return op in DEF_OPS or op in SECTION_OPS

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


def margin_value(pt, margin, root):
    """여백 저장값: hp:switch 의 case(HwpUnitChar) 분기는 pt×100, switch 가 없는 문단 모양과 default 분기는 pt×200.
    근거: 한글 10(5760) 저장 sample_original 은 switch 없이 '  □ '(폭 35pt) 내어쓰기를 -7000 으로 기록(docs/HWPX_STRUCTURE.md)."""
    base = float(pt) * HWPUNIT_PER_PT
    return int(round(base if margin.has_ancestor("case", stop=root) else base * 2))


def op_set_para_space_before(data, root, a):
    edits = []
    for margin in _only(list(root.iter("margin")), "hh:margin"):
        for prev in _only(margin.kids("prev"), "hc:prev"):
            unit = prev.attrs.get("unit", "HWPUNIT")
            if unit != "HWPUNIT":
                raise OpNotApplicable("문단 위 간격 단위가 HWPUNIT 이 아님(%s)" % unit)
            edits.append(set_attr_edit(data, prev, "value", margin_value(a["to"], margin, root)))
    return edits


def op_set_para_hanging_indent(data, root, a):
    # hc:intent 음수 = 내어쓰기. 왼쪽 여백(hc:left)은 바꾸지 않는다.
    edits = []
    for margin in _only(list(root.iter("margin")), "hh:margin"):
        for intent in _only(margin.kids("intent"), "hc:intent"):
            unit = intent.attrs.get("unit", "HWPUNIT")
            if unit != "HWPUNIT":
                raise OpNotApplicable("들여쓰기 단위가 HWPUNIT 이 아님(%s)" % unit)
            edits.append(set_attr_edit(data, intent, "value", margin_value(a["to"], margin, root)))
    return edits


def op_set_para_word_break(data, root, a):
    # 줄 나눔 기준 = hh:breakSetting@breakNonLatinWord(한글 단위)·@breakLatinWord(영어 단위).
    # 한글 저장 HWPX 는 breakNonLatinWord 의 값 이름이 동작과 반대(BREAK_WORD=어절, KEEP_WORD=글자) - 값은 Checker 의 action 이 정한다.
    edits = []
    for el in _only(list(root.iter("breakSetting")), "hh:breakSetting"):
        for attr, value in a["to"].items():
            edits.append(set_attr_edit(data, el, attr, value))
    return edits


def op_set_char_size(data, root, a):
    # 글자 크기 = hh:charPr@height (HWPUNIT, 1pt = 100). 언어별 상대 크기(relSz)는 바꾸지 않는다
    if root.local != "charPr" or "height" not in root.attrs:
        raise OpNotApplicable("hh:charPr 의 height 속성이 없음")
    return [set_attr_edit(data, root, "height", int(round(float(a["to"]) * HWPUNIT_PER_PT)))]


# --- 테두리/배경(borderFill) ---------------------------------------------------
BORDER_SIDE_ELEM = {"left": "leftBorder", "right": "rightBorder", "top": "topBorder", "bottom": "bottomBorder"}


def op_set_cell_border(data, root, a):
    # 셀이 쓰는 hh:borderFill 의 지정한 쪽 선 종류(type)를 바꾼다(기본: NONE = 선 없음)
    edits = []
    for side in a["sides"]:
        name = BORDER_SIDE_ELEM.get(side)
        if not name:
            raise OpNotApplicable("알 수 없는 테두리 쪽: %s" % side)
        for el in _only(root.kids(name), "hh:" + name):
            edits.append(set_attr_edit(data, el, "type", a.get("to", "NONE")))
    return edits


def op_set_para_keep_with_next(data, root, a):
    # hh:breakSetting@keepWithNext (switch 안에 있으면 모든 분기) = "1"/"0"
    to = "1" if a.get("to", True) in (True, 1, "1") else "0"
    return [set_attr_edit(data, el, "keepWithNext", to) for el in _only(list(root.iter("breakSetting")), "hh:breakSetting")]


DEF_OPS = {
    "set_char_spacing": ("charPr", op_set_char_spacing),
    "set_char_ratio": ("charPr", op_set_char_ratio),
    "set_char_font": ("charPr", op_set_char_font),
    "set_char_size": ("charPr", op_set_char_size),
    "set_para_align": ("paraPr", op_set_para_align),
    "set_para_line_spacing": ("paraPr", op_set_para_line_spacing),
    "set_para_space_before": ("paraPr", op_set_para_space_before),
    "set_para_hanging_indent": ("paraPr", op_set_para_hanging_indent),
    "set_para_keep_with_next": ("paraPr", op_set_para_keep_with_next),
    "set_para_word_break": ("paraPr", op_set_para_word_break),
    "set_cell_border": ("borderFill", op_set_cell_border),
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


def mm_to_hwpunit(mm):
    # 한글 저장값 관측: 20mm → 5669, 10mm → 2834, 15mm → 4251 (소수점 이하 버림과 일치)
    return int(float(mm) * HWPUNIT_PER_MM)


def op_set_page_margin(data, page_pr, a):
    # hp:pagePr/hp:margin 의 쪽(top/bottom/left/right/header/footer) 값을 mm → HWPUNIT 으로 바꿔 설정
    margins = page_pr.kids("margin")
    if not margins:
        raise OpNotApplicable("hp:pagePr 에 hp:margin 이 없음")
    edits = []
    for side, mm in a["to_mm"].items():
        if side not in ("top", "bottom", "left", "right", "header", "footer"):
            raise OpNotApplicable("알 수 없는 여백 항목: %s" % side)
        edits.append(set_attr_edit(data, margins[0], side, mm_to_hwpunit(mm)))
    return edits


SECTION_OPS = {
    "set_page_size": op_set_page_size,
    "set_page_margin": op_set_page_margin,
}


def supported(op):
    return op in DEF_OPS or op in SECTION_OPS

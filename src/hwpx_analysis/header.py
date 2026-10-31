"""Contents/header.xml (hh:head) 서식 정의 해석.

OWPML 에서 본문의 hp:p@paraPrIDRef, hp:run@charPrIDRef 는 header 의
hh:paraPr / hh:charPr 를 직접 가리키며, 그 값이 해당 문단/글자에 실제 적용된
완결된 속성 집합이다(스타일 위의 차이값이 아님). 따라서 실제 서식은 참조된
정의를 그대로 해석하면 된다. hp:p@styleIDRef 는 어떤 스타일에서 출발했는지를 나타낸다.

언어 슬롯: fontRef/ratio/spacing/relSz/offset 은 7개 언어별 값을 가진다.
  hangul, latin, hanja, japanese, other, symbol, user
"""

from .units import to_pt, to_mm
from .xmlutil import ln, child, children, to_int, to_bool, resolve_switch_child, effective_children

LANGS = ("hangul", "latin", "hanja", "japanese", "other", "symbol", "user")
FONTFACE_LANG = {  # hh:fontface@lang -> 언어 슬롯
    "HANGUL": "hangul", "LATIN": "latin", "HANJA": "hanja", "JAPANESE": "japanese",
    "OTHER": "other", "SYMBOL": "symbol", "USER": "user",
}


def _lang_values(elem, cast=to_int):
    if elem is None:
        return None
    return {lang: cast(elem.get(lang)) for lang in LANGS}


def _uniform(d):
    """언어별 값이 모두 같으면 그 값을, 아니면 None."""
    if not d:
        return None
    vals = set(d.values())
    return vals.pop() if len(vals) == 1 else None


class HeaderInfo:
    def __init__(self, root):
        self.root = root
        self.warnings = []
        self.fonts = {}          # lang -> {id: {...}}
        self.char_props = {}     # id -> dict
        self.para_props = {}     # id -> dict
        self.styles = {}         # id -> dict
        self.border_fills = {}   # id -> dict
        self.numberings = {}     # id -> dict
        self.bullets = {}        # id -> dict
        self.tab_props = {}      # id -> dict
        self.begin_num = None
        self.unhandled_reflist = []
        self._parse()

    # ------------------------------------------------------------------
    def _scan_switches(self):
        """처리 위치가 정의되지 않았거나 이해하지 못하는 hp:switch 를 경고로 남긴다."""
        from .xmlutil import UNDERSTOOD_CASE_NAMESPACES, REQUIRED_NS_ATTR
        handled_parents = {"paraPr", "tabPr"}
        parent_of = {c: p for p in self.root.iter() for c in p}
        for sw in self.root.iter():
            if ln(sw.tag) != "switch":
                continue
            parent = ln(parent_of[sw].tag)
            nss = [b.get(REQUIRED_NS_ATTR) for b in sw if ln(b.tag) == "case"]
            if parent not in handled_parents:
                self.warnings.append("hh:%s 안의 hp:switch 는 해석하지 않았습니다(case ns=%s)." % (parent, nss))
            elif not any(ns in UNDERSTOOD_CASE_NAMESPACES for ns in nss):
                self.warnings.append("이해하지 못하는 hp:case ns=%s → hp:default 사용 (hh:%s)" % (nss, parent))

    def _parse(self):
        self._scan_switches()
        bn = child(self.root, "beginNum")
        if bn is not None:
            self.begin_num = {k: to_int(v) for k, v in bn.attrib.items()}
        ref = child(self.root, "refList")
        if ref is None:
            self.warnings.append("hh:refList 가 없습니다.")
            return
        handlers = {
            "fontfaces": self._parse_fontfaces,
            "borderFills": self._parse_border_fills,
            "charProperties": self._parse_char_props,
            "tabProperties": self._parse_tab_props,
            "numberings": self._parse_numberings,
            "bullets": self._parse_bullets,
            "paraProperties": self._parse_para_props,
            "styles": self._parse_styles,
        }
        for sect in ref:
            h = handlers.get(ln(sect.tag))
            if h:
                h(sect)
            else:
                self.unhandled_reflist.append(ln(sect.tag))

    def _parse_fontfaces(self, el):
        for ff in children(el, "fontface"):
            lang = FONTFACE_LANG.get(ff.get("lang"), (ff.get("lang") or "").lower())
            fonts = {}
            for f in children(ff, "font"):
                fid = to_int(f.get("id"))
                subst = child(f, "substFont")
                fonts[fid] = {
                    "face": f.get("face"),
                    "type": f.get("type"),
                    "is_embedded": to_bool(f.get("isEmbedded")),
                    "subst_face": subst.get("face") if subst is not None else None,
                }
            self.fonts[lang] = fonts

    def _parse_border_fills(self, el):
        for bf in children(el, "borderFill"):
            bid = to_int(bf.get("id"))
            borders = {}
            for side in ("leftBorder", "rightBorder", "topBorder", "bottomBorder", "diagonal"):
                b = child(bf, side)
                if b is not None:
                    borders[side.replace("Border", "")] = {
                        "type": b.get("type"), "width": b.get("width"), "color": b.get("color"),
                    }
            fill = None
            fb = child(bf, "fillBrush")
            if fb is not None:
                wb = child(fb, "winBrush")
                if wb is not None:
                    fill = {"kind": "color", "face_color": wb.get("faceColor"),
                            "hatch_color": wb.get("hatchColor"), "hatch_style": wb.get("hatchStyle"),
                            "alpha": wb.get("alpha")}
                elif child(fb, "gradation") is not None:
                    fill = {"kind": "gradation"}
                elif child(fb, "imgBrush") is not None:
                    fill = {"kind": "image"}
                else:
                    fill = {"kind": "unknown", "children": [ln(c.tag) for c in fb]}
            self.border_fills[bid] = {"id": bid, "borders": borders, "fill": fill,
                                      "three_d": to_bool(bf.get("threeD")),
                                      "shadow": to_bool(bf.get("shadow"))}

    def font_face(self, lang, fid):
        return (self.fonts.get(lang) or {}).get(fid, {}).get("face")

    def _parse_char_props(self, el):
        for cp in children(el, "charPr"):
            cid = to_int(cp.get("id"))
            font_ref = _lang_values(child(cp, "fontRef"))
            faces = {lang: self.font_face(lang, fid) for lang, fid in (font_ref or {}).items()}
            ratio = _lang_values(child(cp, "ratio"))
            spacing = _lang_values(child(cp, "spacing"))
            rel_sz = _lang_values(child(cp, "relSz"))
            offset = _lang_values(child(cp, "offset"))
            height = to_int(cp.get("height"))
            ul = child(cp, "underline")
            so = child(cp, "strikeout")
            ol = child(cp, "outline")
            sh = child(cp, "shadow")
            # 글자의 실제 크기 = 기준 크기(height) x 언어별 상대 크기(relSz)
            eff = None
            if height is not None and rel_sz:
                eff = {lang: to_pt(height * (v if v is not None else 100) / 100) for lang, v in rel_sz.items()}
            self.char_props[cid] = {
                "id": cid,
                "size_pt": to_pt(height),
                "height_hwpunit": height,
                "effective_size_pt": eff,
                "font_ref": font_ref,
                "font_face": faces,
                "ratio": ratio,          # 장평(%)
                "spacing": spacing,      # 자간(%)
                "rel_size": rel_sz,      # 상대 크기(%)
                "offset": offset,        # 글자 위치(%)
                "bold": child(cp, "bold") is not None,
                "italic": child(cp, "italic") is not None,
                "underline": ul.get("type") if ul is not None else None,
                "strikeout": so.get("shape") if so is not None else None,
                "outline": ol.get("type") if ol is not None else None,
                "shadow": sh.get("type") if sh is not None else None,
                "emboss": child(cp, "emboss") is not None,
                "engrave": child(cp, "engrave") is not None,
                "superscript": child(cp, "supscript") is not None,
                "subscript": child(cp, "subscript") is not None,
                "text_color": cp.get("textColor"),
                "shade_color": cp.get("shadeColor"),
                "use_font_space": to_bool(cp.get("useFontSpace")),
                "use_kerning": to_bool(cp.get("useKerning")),
                "sym_mark": cp.get("symMark"),
                "border_fill_id": to_int(cp.get("borderFillIDRef")),
            }

    def _parse_tab_props(self, el):
        for tp in children(el, "tabPr"):
            tid = to_int(tp.get("id"))
            self.tab_props[tid] = {
                "id": tid,
                "auto_tab_left": to_bool(tp.get("autoTabLeft")),
                "auto_tab_right": to_bool(tp.get("autoTabRight")),
                "items": sum(1 for e, _, _ in effective_children(tp) if ln(e.tag) == "tabItem"),
            }

    def _parse_numberings(self, el):
        for nb in children(el, "numbering"):
            nid = to_int(nb.get("id"))
            heads = {}
            for ph in children(nb, "paraHead"):
                lvl = to_int(ph.get("level"))
                heads[lvl] = {
                    "level": lvl,
                    "num_format": ph.get("numFormat"),
                    "format_text": ph.text,  # 예: '^1.' -> '1.' (^n 은 n수준 번호 자리)
                    "start": to_int(ph.get("start")),
                    "align": ph.get("align"),
                    "char_pr_id": to_int(ph.get("charPrIDRef")),
                }
            self.numberings[nid] = {"id": nid, "start": to_int(nb.get("start")), "para_heads": heads}

    def _parse_bullets(self, el):
        for b in children(el, "bullet"):
            bid = to_int(b.get("id"))
            self.bullets[bid] = {"id": bid, "char": b.get("char"), "use_image": to_bool(b.get("useImage"))}

    def _parse_para_props(self, el):
        for pp in children(el, "paraPr"):
            pid = to_int(pp.get("id"))
            align = resolve_switch_child(pp, "align")[0]
            heading, heading_src, heading_default = resolve_switch_child(pp, "heading")
            brk = resolve_switch_child(pp, "breakSetting")[0]
            auto = resolve_switch_child(pp, "autoSpacing")[0]
            border = resolve_switch_child(pp, "border")[0]
            margin_el, margin_src, margin_default = resolve_switch_child(pp, "margin")
            ls_el, ls_src, ls_default = resolve_switch_child(pp, "lineSpacing")

            # 여백 값의 단위: hp:switch 의 case(2016/HwpUnitChar)는 1pt = 100, switch 가 없는(direct) 문서와 default 분기는
            # 그 2배(1pt = 200). 근거: docs/HWPX_STRUCTURE.md 2장(sample_original: direct -7000 = '  □ ' 14pt 폭 35pt)
            margin, margin_units = self._margin(margin_el, margin_scale(margin_src))
            default_margin = self._margin(margin_default, 100)[0] if margin_default is not None else None   # 원값(÷100) 그대로 병기

            line_spacing = None
            if ls_el is not None:
                ls_type = ls_el.get("type")
                ls_val = to_int(ls_el.get("value"))
                line_spacing = {"type": ls_type, "value": ls_val, "unit": ls_el.get("unit"), "source": ls_src}
                if ls_type == "PERCENT":
                    line_spacing["display"] = "%d%%" % ls_val if ls_val is not None else None
                else:
                    # FIXED / BETWEEN_LINES / AT_LEAST : 값은 HWPUNIT 길이
                    line_spacing["value_pt"] = to_pt(ls_val)
                    line_spacing["display"] = "%spt" % to_pt(ls_val) if ls_val is not None else None
                if ls_default is not None:
                    line_spacing["switch_default_raw"] = {"type": ls_default.get("type"),
                                                          "value": to_int(ls_default.get("value"))}

            self.para_props[pid] = {
                "id": pid,
                "align": align.get("horizontal") if align is not None else None,
                "vertical_align": align.get("vertical") if align is not None else None,
                "heading": {
                    "type": heading.get("type"),
                    "id_ref": to_int(heading.get("idRef")),
                    "level": to_int(heading.get("level")),  # 0 부터 시작(0 = 1수준)
                    "source": heading_src,
                    "switch_default_raw": dict(heading_default.attrib) if heading_default is not None else None,
                } if heading is not None else None,
                "margin": margin,
                "margin_units": margin_units,
                "margin_source": margin_src,
                "margin_switch_default_raw": default_margin,
                "line_spacing": line_spacing,
                "break": {k: brk.get(k) for k in ("breakLatinWord", "breakNonLatinWord", "widowOrphan",
                                                    "keepWithNext", "keepLines", "pageBreakBefore", "lineWrap")}
                if brk is not None else None,
                "auto_spacing": dict(auto.attrib) if auto is not None else None,
                "tab_pr_id": to_int(pp.get("tabPrIDRef")),
                "condense": to_int(pp.get("condense")),
                "font_line_height": to_bool(pp.get("fontLineHeight")),
                "snap_to_grid": to_bool(pp.get("snapToGrid")),
                "border_fill_id": to_int(border.get("borderFillIDRef")) if border is not None else None,
            }

    def _margin(self, margin_el, scale=100):
        """hh:margin -> {indent, left, right, prev, next} (pt). scale = 1pt 당 저장값(case 100, direct/default 200).
        단위가 HWPUNIT 이 아니면 원값을 남긴다."""
        if margin_el is None:
            return None, None
        out, units = {}, set()
        names = {"intent": "indent", "left": "left", "right": "right", "prev": "prev", "next": "next"}
        for tag, key in names.items():
            e = child(margin_el, tag)
            if e is None:
                continue
            v = to_int(e.get("value"))
            unit = e.get("unit") or "HWPUNIT"
            units.add(unit)
            if unit == "HWPUNIT":
                out[key + "_pt"] = None if v is None else round(v / float(scale), 2)
            else:
                out[key + "_raw"] = v
                out[key + "_unit"] = unit
                self.warnings.append("paraPr margin 단위 %s 는 변환하지 않았습니다(원값 보존)." % unit)
        return out, sorted(units)

    def _parse_styles(self, el):
        for st in children(el, "style"):
            sid = to_int(st.get("id"))
            self.styles[sid] = {
                "id": sid,
                "type": st.get("type"),
                "name": st.get("name"),
                "eng_name": st.get("engName"),
                "para_pr_id": to_int(st.get("paraPrIDRef")),
                "char_pr_id": to_int(st.get("charPrIDRef")),
                "next_style_id": to_int(st.get("nextStyleIDRef")),
            }

    # ------------------------------------------------------------------
    def numbering_text(self, numbering_id, level0):
        """개요/번호 문단의 자동 번호 서식 문자열(예: '^1.'). level0 은 0 부터."""
        nb = self.numberings.get(numbering_id)
        if not nb:
            return None
        head = nb["para_heads"].get((level0 or 0) + 1)
        return head and {"format_text": head["format_text"], "num_format": head["num_format"]}

    def to_dict(self):
        return {
            "fonts": {lang: {str(k): v for k, v in fonts.items()} for lang, fonts in self.fonts.items()},
            "char_properties": {str(k): v for k, v in self.char_props.items()},
            "para_properties": {str(k): v for k, v in self.para_props.items()},
            "styles": {str(k): v for k, v in self.styles.items()},
            "border_fills": {str(k): v for k, v in self.border_fills.items()},
            "numberings": {str(k): {**v, "para_heads": {str(l): h for l, h in v["para_heads"].items()}}
                           for k, v in self.numberings.items()},
            "bullets": {str(k): v for k, v in self.bullets.items()},
            "tab_properties": {str(k): v for k, v in self.tab_props.items()},
            "begin_num": self.begin_num,
        }


def margin_scale(source):
    """여백 값 1pt 당 저장값. hp:switch 의 case 분기만 100, 그 외(switch 없음·default)는 200."""
    return 100 if source and str(source).startswith("switch:case") else 200


def summarize_char(cp):
    """문단 요약용: 대표 언어(한글) 기준 값 + 언어별 차이 여부."""
    if cp is None:
        return None
    face = cp["font_face"] or {}
    ratio = cp["ratio"] or {}
    spacing = cp["spacing"] or {}
    eff = cp["effective_size_pt"] or {}
    return {
        "char_pr_id": cp["id"],
        "font_hangul": face.get("hangul"),
        "font_latin": face.get("latin"),
        "size_pt": cp["size_pt"],
        "effective_size_pt_hangul": eff.get("hangul"),
        "ratio_hangul": ratio.get("hangul"),
        "spacing_hangul": spacing.get("hangul"),
        "ratio_uniform": _uniform(ratio) is not None,
        "spacing_uniform": _uniform(spacing) is not None,
        "bold": cp["bold"],
        "italic": cp["italic"],
        "underline": cp["underline"],
        "text_color": cp["text_color"],
    }


def summarize_para(pp):
    if pp is None:
        return None
    m = pp.get("margin") or {}
    ls = pp.get("line_spacing") or {}
    return {
        "para_pr_id": pp["id"],
        "align": pp["align"],
        "line_spacing_type": ls.get("type"),
        "line_spacing_value": ls.get("value"),
        "line_spacing": ls.get("display"),
        "space_before_pt": m.get("prev_pt"),
        "space_after_pt": m.get("next_pt"),
        "indent_pt": m.get("indent_pt"),
        "left_margin_pt": m.get("left_pt"),
        "right_margin_pt": m.get("right_pt"),
        "heading_type": (pp.get("heading") or {}).get("type"),
        "heading_level": (pp.get("heading") or {}).get("level"),
        # 줄 나눔 기준(hh:breakSetting). 한글 저장 파일은 한글 단위의 값 이름이 동작과 반대(BREAK_WORD=어절, KEEP_WORD=글자)
        "keep_with_next": (pp.get("break") or {}).get("keepWithNext"),
        "break_hangul": (pp.get("break") or {}).get("breakNonLatinWord"),
        "break_latin": (pp.get("break") or {}).get("breakLatinWord"),
        "value_source": pp.get("margin_source"),
        "margin_scale": margin_scale(pp.get("margin_source")),
    }

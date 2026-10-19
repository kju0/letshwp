"""Contents/sectionN.xml (hs:sec) 본문 순회.

본문 트리(샘플 및 OWPML 구조 기준):
  hs:sec
    hp:p (paraPrIDRef, styleIDRef, pageBreak, columnBreak)
      hp:run (charPrIDRef)
        hp:t            텍스트. 혼합 내용으로 hp:tab, hp:lineBreak, hp:fwSpace 등을 포함
        hp:secPr        구역 설정(첫 문단의 첫 run) - hp:pagePr/hp:margin 용지·여백
        hp:ctrl         colPr, header, footer, footNote, endNote, pageNum, fieldBegin ...
        hp:tbl          표 -> hp:tr -> hp:tc -> hp:subList -> hp:p ...
        hp:pic          그림 (hc:img@binaryItemIDRef)
        hp:rect 등      도형 (hp:drawText -> hp:subList 에 글상자 문단)
        hp:container    묶음 개체
      hp:linesegarray   레이아웃 캐시(줄 위치). 서식 분석에는 사용하지 않음

모든 hp:p 를 문서 순서대로 방문하며, 문단마다 어떤 컨테이너(본문/표 셀/머리말/
각주/글상자/캡션) 안에 있는지 문맥을 기록한다. 처리 방법을 모르는 요소도
내부의 hp:subList 를 찾아 문단을 누락하지 않도록 하고, 해당 태그를 '미지원'으로 집계한다.
"""

from collections import Counter

from .header import summarize_char, summarize_para
from .units import to_mm, detect_paper
from .xmlutil import ln, child, children, to_int, to_bool

# hp:t 혼합 내용의 인라인 요소 -> 텍스트 표현
INLINE_TEXT = {
    "tab": "\t",
    "lineBreak": "\n",
    "fwSpace": " ",      # 고정폭 빈칸
    "nbSpace": " ",  # 묶음 빈칸
    "hyphen": "-",
}
# 텍스트를 만들지 않는 표식(위치 표시) 요소
INLINE_MARKERS = {"titleMark", "markpenBegin", "markpenEnd", "insertBegin", "insertEnd",
                  "deleteBegin", "deleteEnd", "indexmark"}

SHAPE_TAGS = {"rect", "ellipse", "arc", "polygon", "curve", "connectLine", "line", "textart"}
# 존재는 기록하되 내부 서식은 분석하지 않는 개체
OTHER_OBJECT_TAGS = {"equation", "ole", "chart", "video", "compose", "dutmal",
                     "btn", "radioBtn", "checkBtn", "comboBox", "edit", "listBox", "scrollBar"}
CTRL_WITH_TEXT = {"header": "header", "footer": "footer", "footNote": "footnote",
                  "endNote": "endnote", "hiddenComment": "memo"}

SENTINEL_UINT32 = 4294967295


def extract_t_text(t, unknown_counter):
    parts = [t.text or ""]
    for c in t:
        name = ln(c.tag)
        if name in INLINE_TEXT:
            parts.append(INLINE_TEXT[name])
        elif name in INLINE_MARKERS:
            pass
        else:
            unknown_counter["t/" + name] += 1
        parts.append(c.tail or "")
    return "".join(parts)


def _size(el):
    if el is None:
        return None
    w, h = to_int(el.get("width")), to_int(el.get("height"))
    return {"width_mm": to_mm(w), "height_mm": to_mm(h)}


def _pos(el):
    if el is None:
        return None
    return {
        "treat_as_char": to_bool(el.get("treatAsChar")),
        "vert_rel_to": el.get("vertRelTo"), "horz_rel_to": el.get("horzRelTo"),
        "vert_align": el.get("vertAlign"), "horz_align": el.get("horzAlign"),
    }


def _margins(el):
    if el is None:
        return None
    return {k + "_mm": to_mm(to_int(el.get(k))) for k in ("left", "right", "top", "bottom")}


class SectionWalker:
    def __init__(self, header, binary_items, section_index, state):
        self.h = header
        self.bin = binary_items
        self.sec = section_index
        self.st = state  # 문서 전체 공유 상태(카운터, 결과 리스트)

    # ------------------------------------------------------------------
    def walk(self, root):
        sec_info = {"index": self.sec, "properties": None, "header_footer": [], "page_numbers": []}
        self.st["sections"].append(sec_info)
        self._cur_sec = sec_info
        ctx = {"container": "body", "path": "sec%d" % self.sec, "depth": 0,
               "table_id": None, "cell": None, "object_id": None}
        for i, p in enumerate(children(root, "p")):
            self.visit_paragraph(p, dict(ctx, path="%s/p%d" % (ctx["path"], i)))
        for c in root:
            if ln(c.tag) != "p":
                self.st["unknown"]["sec/" + ln(c.tag)] += 1
        return sec_info

    # ------------------------------------------------------------------
    def visit_paragraph(self, p, ctx):
        idx = len(self.st["paragraphs"])
        rec = {"index": idx, "section": self.sec, "path": ctx["path"],
               "context": {k: ctx[k] for k in ("container", "depth", "table_id", "cell", "object_id")}}
        self.st["paragraphs"].append(rec)  # 순서 보존을 위해 먼저 자리를 잡는다
        self.st["xml_counts_visited"]["p"] += 1

        para_pr_id = to_int(p.get("paraPrIDRef"))
        style_id = to_int(p.get("styleIDRef"))
        run_info = []
        text_parts = []
        objects = []
        controls = []
        local_obj_count = Counter()

        for r_i, run in enumerate(children(p, "run")):
            cpid = to_int(run.get("charPrIDRef"))
            run_text = []
            for c in run:
                name = ln(c.tag)
                if name == "t":
                    s = extract_t_text(c, self.st["unknown"])
                    run_text.append(s)
                elif name == "secPr":
                    self._cur_sec["properties"] = self.parse_sec_pr(c)
                    controls.append("secPr")
                elif name == "ctrl":
                    for cc in c:
                        controls.append(ln(cc.tag))
                        oid = self.handle_ctrl(cc, ctx, idx)
                        if oid:
                            objects.append(oid)
                elif name == "tbl":
                    k = local_obj_count["tbl"]; local_obj_count["tbl"] += 1
                    objects.append(self.handle_table(c, ctx, idx, "%s/tbl%d" % (ctx["path"], k)))
                elif name == "pic":
                    k = local_obj_count["pic"]; local_obj_count["pic"] += 1
                    objects.append(self.handle_pic(c, ctx, idx, "%s/pic%d" % (ctx["path"], k)))
                elif name in SHAPE_TAGS or name == "container":
                    k = local_obj_count[name]; local_obj_count[name] += 1
                    objects.append(self.handle_shape(c, ctx, idx, "%s/%s%d" % (ctx["path"], name, k)))
                elif name in OTHER_OBJECT_TAGS:
                    k = local_obj_count[name]; local_obj_count[name] += 1
                    objects.append(self.handle_other(c, ctx, idx, "%s/%s%d" % (ctx["path"], name, k)))
                else:
                    self.st["unknown"]["run/" + name] += 1
                    self._visit_nested_sublists(c, ctx, "%s/%s" % (ctx["path"], name), "unknown:" + name)
            t = "".join(run_text)
            text_parts.append(t)
            run_info.append({"char_pr_id": cpid, "text": t, "chars": len(t),
                             "visible_chars": len(t.strip())})
        for c in p:
            if ln(c.tag) not in ("run", "linesegarray"):
                self.st["unknown"]["p/" + ln(c.tag)] += 1

        text = "".join(text_parts)
        pp = self.h.para_props.get(para_pr_id)
        if pp is None:
            self.st["warnings"].append("문단 %d: paraPrIDRef=%s 정의 없음" % (idx, para_pr_id))
        style = self.h.styles.get(style_id)

        # 대표 글자 모양: 공백 제외 글자 수가 가장 많은 charPr. 글자가 없으면 첫 run.
        usage = Counter()
        for ri in run_info:
            if ri["char_pr_id"] is not None:
                usage[ri["char_pr_id"]] += ri["visible_chars"]
                if ri["char_pr_id"] not in self.h.char_props:
                    self.st["warnings"].append("문단 %d: charPrIDRef=%s 정의 없음" % (idx, ri["char_pr_id"]))
        visible = [k for k, v in usage.items() if v > 0]
        if visible:
            dominant = max(visible, key=lambda k: usage[k])
        else:
            dominant = run_info[0]["char_pr_id"] if run_info else None
        dom_cp = self.h.char_props.get(dominant)
        distinct_formats = self._distinct_char_formats(visible)

        rec.update({
            "text": text,
            "text_length": len(text),
            "visible_length": len(text.strip()),
            "is_empty": len(text.strip()) == 0,
            "style": {"id": style_id, "name": style["name"] if style else None,
                      "eng_name": style["eng_name"] if style else None},
            "page_break_before": to_bool(p.get("pageBreak")),
            "column_break_before": to_bool(p.get("columnBreak")),
            "para_format": summarize_para(pp),
            "char_format": summarize_char(dom_cp),
            "char_format_mixed": len(distinct_formats) > 1,
            "char_format_mixed_detail": distinct_formats if len(distinct_formats) > 1 else None,
            "runs": [{"char_pr_id": r["char_pr_id"], "chars": r["chars"]} for r in run_info],
            "auto_numbering": self._auto_numbering(pp),
            "objects": objects,
            "controls": controls,
        })
        return idx

    def _distinct_char_formats(self, cp_ids):
        """서로 다른 charPr id 라도 분석 대상 속성(글꼴/크기/장평/자간/굵게/기울임)이 같으면 하나로 본다."""
        seen = {}
        for cid in cp_ids:
            cp = self.h.char_props.get(cid)
            if not cp:
                continue
            s = summarize_char(cp)
            key = (s["font_hangul"], s["font_latin"], s["size_pt"], s["ratio_hangul"],
                   s["spacing_hangul"], s["bold"], s["italic"])
            seen.setdefault(key, {"char_pr_ids": [], "font_hangul": key[0], "size_pt": key[2],
                                  "ratio": key[3], "spacing": key[4], "bold": key[5]})
            seen[key]["char_pr_ids"].append(cid)
        return list(seen.values())

    def _auto_numbering(self, pp):
        """문단 모양의 heading 설정에 따른 자동 번호/글머리표(본문 텍스트에는 나타나지 않음)."""
        if not pp or not pp.get("heading"):
            return None
        hd = pp["heading"]
        htype = hd.get("type")
        if htype in (None, "NONE"):
            return None
        out = {"type": htype, "level": (hd.get("level") or 0) + 1, "id_ref": hd.get("id_ref")}
        if htype == "OUTLINE":
            # 개요 번호 모양은 구역의 secPr@outlineShapeIDRef 가 가리키는 numbering 을 따른다.
            props = self._cur_sec.get("properties") or {}
            nid = props.get("outline_shape_id")
            out["numbering_id"] = nid
            out["numbering_source"] = "secPr@outlineShapeIDRef"
            fmt = self.h.numbering_text(nid, hd.get("level")) if nid is not None else None
            out.update(fmt or {})
        elif htype == "NUMBER":
            out["numbering_id"] = hd.get("id_ref")
            out.update(self.h.numbering_text(hd.get("id_ref"), hd.get("level")) or {})
        elif htype == "BULLET":
            b = self.h.bullets.get(hd.get("id_ref"))
            out["bullet_char"] = b["char"] if b else None
        return out

    # ------------------------------------------------------------------
    def parse_sec_pr(self, sp):
        page = child(sp, "pagePr")
        out = {
            "text_direction": sp.get("textDirection"),
            "outline_shape_id": to_int(sp.get("outlineShapeIDRef")),
            "space_columns_mm": to_mm(to_int(sp.get("spaceColumns"))),
            "tab_stop_mm": to_mm(to_int(sp.get("tabStop"))),
        }
        if page is not None:
            w, h = to_int(page.get("width")), to_int(page.get("height"))
            wmm, hmm = to_mm(w), to_mm(h)
            m = child(page, "margin")
            out["page"] = {
                "width_mm": wmm, "height_mm": hmm,
                "width_hwpunit": w, "height_hwpunit": h,
                "paper": detect_paper(wmm, hmm),
                "orientation_by_dimensions": None if w is None or h is None else
                ("PORTRAIT" if w < h else "LANDSCAPE" if w > h else "SQUARE"),
                "landscape_attr_raw": page.get("landscape"),
                "gutter_type": page.get("gutterType"),
                "margin_mm": {k: to_mm(to_int(m.get(k))) for k in
                              ("left", "right", "top", "bottom", "header", "footer", "gutter")}
                if m is not None else None,
            }
        g = child(sp, "grid")
        if g is not None:
            out["grid"] = dict(g.attrib)
        sn = child(sp, "startNum")
        if sn is not None:
            out["start_num"] = dict(sn.attrib)
        for np_name in ("footNotePr", "endNotePr"):
            np_ = child(sp, np_name)
            if np_ is not None:
                anf = child(np_, "autoNumFormat")
                out[np_name] = {"number_format": anf.get("type") if anf is not None else None,
                                "suffix": anf.get("suffixChar") if anf is not None else None}
        out["page_border_fills"] = len(children(sp, "pageBorderFill"))
        return out

    # ------------------------------------------------------------------
    def handle_ctrl(self, cc, ctx, para_idx):
        name = ln(cc.tag)
        if name == "colPr":
            if ctx["container"] == "body":
                self._cur_sec.setdefault("columns", []).append({
                    "paragraph": para_idx, "type": cc.get("type"), "count": to_int(cc.get("colCount")),
                    "same_size": to_bool(cc.get("sameSz"))})
            return None
        if name == "pageNum":
            self._cur_sec["page_numbers"].append({"paragraph": para_idx, "pos": cc.get("pos"),
                                                  "format": cc.get("formatType"),
                                                  "side_char": cc.get("sideChar")})
            return None
        if name in CTRL_WITH_TEXT:
            kind = CTRL_WITH_TEXT[name]
            oid = "%s%d" % (kind.upper()[:3], self.st["counters"][kind])
            self.st["counters"][kind] += 1
            info = {"id": oid, "kind": kind, "paragraph": para_idx,
                    "apply_page_type": cc.get("applyPageType"), "paragraphs": []}
            if kind in ("header", "footer"):
                self._cur_sec["header_footer"].append(info)
            else:
                self.st["notes"].append(info)
            sub_ctx = dict(ctx, container=kind, depth=ctx["depth"] + 1, object_id=oid,
                           table_id=None, cell=None)
            for sl in children(cc, "subList"):
                for j, p in enumerate(children(sl, "p")):
                    info["paragraphs"].append(
                        self.visit_paragraph(p, dict(sub_ctx, path="%s/%s/p%d" % (ctx["path"], oid, j))))
            return oid
        if name in ("fieldBegin", "fieldEnd", "bookmark", "autoNum", "newNum", "pageHiding",
                    "pageNumCtrl", "indexmark"):
            if name == "fieldBegin":
                self.st["fields"][cc.get("type") or "?"] += 1
            return None
        self.st["unknown"]["ctrl/" + name] += 1
        self._visit_nested_sublists(cc, ctx, "%s/ctrl:%s" % (ctx["path"], name), "unknown:" + name)
        return None

    # ------------------------------------------------------------------
    def _caption(self, obj_el, ctx, oid, parent_kind):
        cap = child(obj_el, "caption")
        if cap is None:
            return None
        info = {"side": cap.get("side"), "full_size": to_bool(cap.get("fullSz")),
                "gap_mm": to_mm(to_int(cap.get("gap"))), "paragraphs": []}
        sub_ctx = dict(ctx, container="caption", depth=ctx["depth"] + 1, object_id=oid,
                       table_id=None, cell=None, caption_of=parent_kind)
        for sl in children(cap, "subList"):
            for j, p in enumerate(children(sl, "p")):
                i = self.visit_paragraph(p, dict(sub_ctx, path="%s/%s/caption/p%d" % (ctx["path"], oid, j)))
                self.st["paragraphs"][i]["context"]["caption_of"] = parent_kind
                info["paragraphs"].append(i)
        return info

    def _common_obj(self, el):
        sz = child(el, "sz")
        return {
            "xml_id": el.get("id"),
            "numbering_type": el.get("numberingType"),
            "text_wrap": el.get("textWrap"),
            "z_order": to_int(el.get("zOrder")),
            "size": _size(sz),
            "position": _pos(child(el, "pos")),
            "out_margin": _margins(child(el, "outMargin")),
        }

    def handle_table(self, tbl, ctx, para_idx, path):
        self.st["xml_counts_visited"]["tbl"] += 1
        tid = "T%d" % self.st["counters"]["table"]
        self.st["counters"]["table"] += 1
        bf_id = to_int(tbl.get("borderFillIDRef"))
        rec = {
            "id": tid, "path": path, "anchor_paragraph": para_idx,
            "context": ctx["container"], "nesting_depth": ctx["depth"],
            "parent_table": ctx.get("table_id"),
            "rows": to_int(tbl.get("rowCnt")), "cols": to_int(tbl.get("colCnt")),
            "cell_spacing_mm": to_mm(to_int(tbl.get("cellSpacing"))),
            "repeat_header": to_bool(tbl.get("repeatHeader")),
            "page_break": tbl.get("pageBreak"),
            "border_fill_id": bf_id,
            "in_margin": _margins(child(tbl, "inMargin")),
            **self._common_obj(tbl),
            "cells": [],
            "cell_zones": [dict(z.attrib) for zl in children(tbl, "cellzoneList") for z in children(zl, "cellzone")],
        }
        self.st["tables"].append(rec)
        rec["caption"] = self._caption(tbl, ctx, tid, "table")
        table_in_margin = child(tbl, "inMargin")

        for tr in children(tbl, "tr"):
            for tc in children(tr, "tc"):
                addr = child(tc, "cellAddr")
                span = child(tc, "cellSpan")
                row = to_int(addr.get("rowAddr")) if addr is not None else None
                col = to_int(addr.get("colAddr")) if addr is not None else None
                sl = child(tc, "subList")
                cbf = to_int(tc.get("borderFillIDRef"))
                bf = self.h.border_fills.get(cbf) or {}
                has_margin = to_bool(tc.get("hasMargin"))
                cell = {
                    "row": row, "col": col,
                    "row_span": to_int(span.get("rowSpan"), 1) if span is not None else 1,
                    "col_span": to_int(span.get("colSpan"), 1) if span is not None else 1,
                    "size": _size(child(tc, "cellSz")),
                    "is_header": to_bool(tc.get("header")),
                    "vert_align": sl.get("vertAlign") if sl is not None else None,
                    "border_fill_id": cbf,
                    "fill": bf.get("fill"),
                    "borders": bf.get("borders"),
                    # hasMargin=0 이면 표의 inMargin 을, 1 이면 셀 자체 cellMargin 을 사용
                    "margin": _margins(child(tc, "cellMargin")) if has_margin else _margins(table_in_margin),
                    "margin_source": "cellMargin" if has_margin else "table.inMargin",
                    "paragraphs": [],
                }
                cctx = dict(ctx, container="table_cell", depth=ctx["depth"] + 1, table_id=tid,
                            cell=[row, col], object_id=tid)
                if sl is not None:
                    for j, p in enumerate(children(sl, "p")):
                        cell["paragraphs"].append(self.visit_paragraph(
                            p, dict(cctx, path="%s/r%dc%d/p%d" % (path, row or 0, col or 0, j))))
                cell["text"] = "\n".join(self.st["paragraphs"][i]["text"] for i in cell["paragraphs"])
                rec["cells"].append(cell)
        rec["grid_check"] = self._grid_check(rec)
        return tid

    @staticmethod
    def _grid_check(rec):
        """셀 병합(span)이 rows x cols 격자를 겹침/빈칸 없이 정확히 덮는지 확인.
        추후 서식 변경 후 '표 구조 불변' 검증의 기준값으로 사용한다."""
        rows, cols = rec["rows"], rec["cols"]
        if not rows or not cols:
            return {"ok": False, "reason": "rowCnt/colCnt 없음"}
        grid = [[0] * cols for _ in range(rows)]
        for c in rec["cells"]:
            if c["row"] is None or c["col"] is None:
                return {"ok": False, "reason": "cellAddr 없음"}
            for r in range(c["row"], c["row"] + c["row_span"]):
                for k in range(c["col"], c["col"] + c["col_span"]):
                    if r >= rows or k >= cols:
                        return {"ok": False, "reason": "span 이 격자를 벗어남 (r%d c%d)" % (r, k)}
                    grid[r][k] += 1
        overlaps = sum(1 for row in grid for v in row if v > 1)
        holes = sum(1 for row in grid for v in row if v == 0)
        merged = sum(1 for c in rec["cells"] if c["row_span"] > 1 or c["col_span"] > 1)
        return {"ok": overlaps == 0 and holes == 0, "cell_count": len(rec["cells"]),
                "merged_cells": merged, "overlaps": overlaps, "holes": holes}

    def handle_pic(self, pic, ctx, para_idx, path):
        self.st["xml_counts_visited"]["pic"] += 1
        pid = "G%d" % self.st["counters"]["picture"]
        self.st["counters"]["picture"] += 1
        img = child(pic, "img")
        ref = img.get("binaryItemIDRef") if img is not None else None
        item = self.bin.get(ref) if ref else None
        sc = child(pic, "shapeComment")
        rec = {
            "id": pid, "path": path, "anchor_paragraph": para_idx, "context": ctx["container"],
            "in_group": ctx["container"] == "group",
            **self._common_obj(pic),
            "binary_item_id": ref,
            "binary_path": item["href"] if item else None,
            "media_type": item["media-type"] if item else None,
            "original_size": _size(child(pic, "orgSz")),
            "current_size": _size(child(pic, "curSz")),
            "shape_comment": (sc.text or "").strip() if sc is not None else None,
        }
        if ref and not item:
            self.st["warnings"].append("그림 %s: binaryItemIDRef=%s 가 manifest 에 없음" % (pid, ref))
        self.st["pictures"].append(rec)
        rec["caption"] = self._caption(pic, ctx, pid, "picture")
        return pid

    def handle_shape(self, el, ctx, para_idx, path):
        name = ln(el.tag)
        self.st["xml_counts_visited"][name] += 1
        sid = "S%d" % self.st["counters"]["shape"]
        self.st["counters"]["shape"] += 1
        rec = {"id": sid, "kind": name, "path": path, "anchor_paragraph": para_idx,
               "context": ctx["container"], **self._common_obj(el),
               "current_size": _size(child(el, "curSz")), "text_paragraphs": [], "members": []}
        fb = child(el, "fillBrush")
        if fb is not None and child(fb, "winBrush") is not None:
            rec["fill_color"] = child(fb, "winBrush").get("faceColor")
        self.st["shapes"].append(rec)
        rec["caption"] = self._caption(el, ctx, sid, "shape")
        dt = child(el, "drawText")
        if dt is not None:
            tctx = dict(ctx, container="textbox", depth=ctx["depth"] + 1, object_id=sid,
                        table_id=None, cell=None)
            for sl in children(dt, "subList"):
                for j, p in enumerate(children(sl, "p")):
                    rec["text_paragraphs"].append(
                        self.visit_paragraph(p, dict(tctx, path="%s/text/p%d" % (path, j))))
        if name == "container":
            gctx = dict(ctx, container="group", depth=ctx["depth"] + 1, object_id=sid)
            k = Counter()
            for m in el:
                mname = ln(m.tag)
                if mname == "pic":
                    rec["members"].append(self.handle_pic(m, gctx, para_idx, "%s/pic%d" % (path, k[mname])))
                elif mname in SHAPE_TAGS or mname == "container":
                    rec["members"].append(self.handle_shape(m, gctx, para_idx, "%s/%s%d" % (path, mname, k[mname])))
                elif mname in OTHER_OBJECT_TAGS:
                    rec["members"].append(self.handle_other(m, gctx, para_idx, "%s/%s%d" % (path, mname, k[mname])))
                k[mname] += 1
        return sid

    def handle_other(self, el, ctx, para_idx, path):
        name = ln(el.tag)
        self.st["xml_counts_visited"][name] += 1
        oid = "O%d" % self.st["counters"]["other"]
        self.st["counters"]["other"] += 1
        rec = {"id": oid, "kind": name, "path": path, "anchor_paragraph": para_idx,
               "context": ctx["container"], **self._common_obj(el),
               "note": "개체 존재만 기록. 내부 서식은 분석하지 않음(미지원)."}
        self.st["other_objects"].append(rec)
        self._visit_nested_sublists(el, ctx, path, "object:" + name)
        return oid

    def _visit_nested_sublists(self, el, ctx, path, container):
        """처리 방법을 모르는 요소 안의 문단도 누락하지 않도록 최상위 hp:subList 를 찾아 방문."""
        def find(e):
            for c in e:
                if ln(c.tag) == "subList":
                    yield c
                elif ln(c.tag) not in ("tbl", "pic") and ln(c.tag) not in SHAPE_TAGS:
                    yield from find(c)
        for n, sl in enumerate(find(el)):
            sctx = dict(ctx, container=container, depth=ctx["depth"] + 1)
            for j, p in enumerate(children(sl, "p")):
                self.visit_paragraph(p, dict(sctx, path="%s/sub%d/p%d" % (path, n, j)))


def new_state():
    return {
        "paragraphs": [], "tables": [], "pictures": [], "shapes": [], "other_objects": [],
        "notes": [], "sections": [],
        "counters": Counter(), "unknown": Counter(), "fields": Counter(),
        "xml_counts_visited": Counter(), "warnings": [],
    }


def count_xml_elements(root, names=("p", "tbl", "pic", "t")):
    c = Counter()
    for e in root.iter():
        n = ln(e.tag)
        if n in names:
            c[n] += 1
    return c


def raw_text_of(root, unknown):
    """검증용: 섹션의 모든 hp:t 텍스트를 순서대로 연결."""
    return "".join(extract_t_text(t, unknown) for t in root.iter() if ln(t.tag) == "t")

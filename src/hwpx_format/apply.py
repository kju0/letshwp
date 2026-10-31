"""fix_plan 의 수정 동작을 HWPX 복제본에 적용한다.

원칙
- 기존 charPr/paraPr 정의는 고치지 않는다. 수정 대상 문단이 쓰는 정의를 복제해 새 id 로 추가하고,
  대상 문단(또는 그 문단의 run)만 새 id 를 참조하게 바꾼다. 그래서 대상이 아닌 문단·스타일의 서식은 그대로다.
- 같은 정의를 쓰는 대상 문단이라도 적용할 op 묶음이 다르면 묶음별로 따로 복제한다.
- 표 셀 테두리(borderFill)도 같은 방식으로 복제해 대상 셀(hp:tc)만 새 id 를 참조하게 한다.
- 구역 설정(쪽 여백 등)은 해당 구역의 hp:pagePr 값을 직접 바꾼다.
- 대제목 쪽 나눔은 hp:p@pageBreak 만 바꾼다. 그 바로 앞 빈 문단 삭제는 문단 구조를 바꾸는 유일한 예외(검증에서 이 경우만 허용).
- XML 은 바이트 단위로 필요한 속성만 바꾼다(xmlpatch). 텍스트·문단·표·그림 요소는 건드리지 않는다.
"""

import zipfile
from collections import OrderedDict, defaultdict

from hwpx_analysis.header import HeaderInfo
from hwpx_analysis.package import HwpxPackage
from hwpx_analysis.section import SectionWalker, new_state
from hwpx_analysis.xmlutil import ln

from .ops import DEF_OPS, SECTION_OPS, OpNotApplicable
from .xmlpatch import scan, set_attr_edit, apply_edits, XmlPatchError

REF_ATTR = {"charPr": "charPrIDRef", "paraPr": "paraPrIDRef", "borderFill": "borderFillIDRef"}
CONTAINER = {"charPr": "charProperties", "paraPr": "paraProperties", "borderFill": "borderFills"}
ID_KEY = {"charPr": "char_pr_id", "paraPr": "para_pr_id", "borderFill": "border_fill_id"}
# 복제 대상 정의의 적용 단위: charPr/paraPr 는 문단(run), borderFill 은 표 셀(hp:tc)
TARGET_UNIT = {"charPr": "paragraph", "paraPr": "paragraph", "borderFill": "cell"}


class ApplyError(RuntimeError):
    pass


# --- 문단(hp:p) 자체에 적용하는 동작: 함수(구역 바이트, hp:p Elem, action) -> 편집 목록 ---
def op_set_page_break(data, p_el, a):
    return [set_attr_edit(data, p_el, "pageBreak", "1" if a.get("to", True) in (True, "1", 1) else "0")]


def op_delete_empty_paragraph(data, p_el, a):
    """빈 문단 삭제 - 절대 원칙의 유일한 예외. 텍스트·개체·컨트롤이 하나라도 있으면 적용하지 않는다."""
    for local in ("tbl", "pic", "ctrl", "secPr", "container", "line", "rect", "ellipse", "arc", "polygon", "curve",
                  "connectLine", "textart", "ole", "equation", "video", "chart", "fieldBegin", "fieldEnd", "footNote", "endNote"):
        if list(p_el.iter(local)):
            raise OpNotApplicable("빈 문단이 아님(hp:%s 포함) - 삭제하지 않음" % local)
    for t in p_el.iter("t"):
        if data[t.stag_end:t.end].strip() not in (b"", b"</hp:t>") and b"<" not in data[t.stag_end:t.end].strip()[:1]:
            raise OpNotApplicable("빈 문단이 아님(글자 있음) - 삭제하지 않음")
        inner = data[t.stag_end:t.end]
        close = inner.rfind(b"</")
        text = inner[:close] if close >= 0 else inner
        if text.strip():
            raise OpNotApplicable("빈 문단이 아님(글자 있음) - 삭제하지 않음")
    return [(p_el.start, p_el.end, b"")]


def op_replace_marker(data, p_el, a):
    """문단 첫 글자(앞 공백 다음)의 항목 번호·기호 문자만 바꾼다. 텍스트 불변 원칙의 예외.
    번호가 여러 run 의 hp:t 에 나뉘어 있으면(예: ' 다' + '. …') 길이가 같을 때 조각별로 바꾼다."""
    old, new = a["from"], a["to"]
    segs = []   # (글자 문자열, 바이트 시작) - 문서 순서의 hp:t 본문
    for t in p_el.iter("t"):
        inner = data[t.stag_end:t.end]
        close = inner.rfind(b"</")
        body = inner[:close] if close >= 0 else inner
        if b"<" in body:
            if segs:
                break
            raise OpNotApplicable("번호·기호가 있어야 할 hp:t 에 다른 요소가 섞여 있음")
        segs.append((body.decode("utf-8"), t.stag_end))
        if sum(len(x) for x, _ in segs) >= len(old) + 64:
            break
    if not segs:
        raise OpNotApplicable("번호·기호가 든 hp:t 가 없음")
    text = "".join(x for x, _ in segs)
    stripped = text.lstrip()
    if not stripped.startswith(old):
        raise OpNotApplicable("문단 첫 글자가 '%s' 가 아님('%s')" % (old, stripped[:5]))
    lead = len(text) - len(stripped)
    # 글자 위치 -> (세그먼트, 세그먼트 안 위치)
    spans, pos = [], 0
    for seg_text, seg_start in segs:
        spans.append((pos, pos + len(seg_text), seg_text, seg_start))
        pos += len(seg_text)
    edits = []
    for s0, e0, seg_text, seg_start in spans:
        lo, hi = max(s0, lead), min(e0, lead + len(old))
        if lo >= hi:
            continue
        if s0 <= lead and lead + len(old) <= e0:
            # 한 세그먼트 안에 다 들어 있음: 길이가 달라도 통째로 교체
            bs = seg_start + len(seg_text[:lead - s0].encode("utf-8"))
            return [(bs, bs + len(old.encode("utf-8")), new.encode("utf-8"))]
        if len(old) != len(new):
            raise OpNotApplicable("번호가 여러 run 에 나뉘어 있고 길이가 달라('%s'→'%s') 바꿀 수 없음" % (old, new))
        part_old, part_new = old[lo - lead:hi - lead], new[lo - lead:hi - lead]
        bs = seg_start + len(seg_text[:lo - s0].encode("utf-8"))
        edits.append((bs, bs + len(part_old.encode("utf-8")), part_new.encode("utf-8")))
    return edits


PARAGRAPH_OPS = {"set_page_break": op_set_page_break, "delete_empty_paragraph": op_delete_empty_paragraph,
                 "replace_marker": op_replace_marker}


def op_add_page_number(data, root, a):
    """첫 문단의 구역 설정(hp:secPr) run 끝에 쪽 번호 컨트롤을 추가한다(sample_original_wannabe 와 같은 위치). 구조 불변 원칙의 예외."""
    sec_prs = list(root.iter("secPr"))
    if not sec_prs:
        raise OpNotApplicable("hp:secPr 가 없어 쪽 번호를 넣을 위치를 정할 수 없음")
    run = sec_prs[0].parent
    if run is None or run.local != "run":
        raise OpNotApplicable("hp:secPr 가 run 안에 있지 않음")
    if list(root.iter("pageNum")):
        raise OpNotApplicable("쪽 번호 컨트롤이 이미 있음")
    prefix = sec_prs[0].qname.split(":")[0] + ":" if ":" in sec_prs[0].qname else ""
    to = a["to"]
    xml = ('<%(p)sctrl><%(p)spageNum pos="%(pos)s" formatType="%(fmt)s" sideChar="%(side)s"/></%(p)sctrl>'
           % {"p": prefix, "pos": to["pos"], "fmt": to["format"], "side": to["side_char"]}).encode("utf-8")
    at = run.children[-1].end if run.children else run.stag_end
    return [(at, at, xml)]



def select_actions(check):
    """적용 대상: '자동 수정'(auto_actions) + 역할이 확인된 '분석 후 수정'(role_based_actions)."""
    fp = check.get("fix_plan") or {}
    out = []
    for group in ("auto_actions", "role_based_actions"):
        for a in fp.get(group) or []:
            out.append(dict(a, plan_group=group))
    return out


def _paragraph_elements(path):
    """Inspector 와 같은 방문 규칙으로 '문단 경로 -> 구역 내 hp:p 순번' 을 만든다."""
    out = []
    with HwpxPackage(path) as pkg:
        desc = pkg.describe()
        header = HeaderInfo(pkg.read_xml(desc["header_part"]))
        state = new_state()
        for i, part in enumerate(desc["section_parts"]):
            root = pkg.read_xml(part)
            found = {}

            class Recorder(SectionWalker):
                def visit_paragraph(self, p, ctx):
                    found[ctx["path"]] = p
                    return super().visit_paragraph(p, ctx)

            Recorder(header, desc["binary_items"], i, state).walk(root)
            order = {id(e): n for n, e in enumerate(e for e in root.iter() if ln(e.tag) == "p")}
            out.append((part, {path: order[id(el)] for path, el in found.items()}))
        return desc["header_part"], out


def _read_zip(path):
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        return infos, {i.filename: z.read(i.filename) for i in infos}


def write_zip(out_path, infos, data):
    """원본과 같은 엔트리 순서·압축 방식으로 쓴다(mimetype 은 첫 엔트리, 무압축 유지)."""
    with zipfile.ZipFile(out_path, "w") as z:
        for info in infos:
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            zi.compress_type = info.compress_type
            zi.external_attr = info.external_attr
            zi.create_system = info.create_system
            zi.comment = info.comment
            zi.extra = info.extra
            z.writestr(zi, data[info.filename])


def _def_index(header_bytes):
    root, _ = scan(header_bytes)
    defs = {k: {} for k in CONTAINER}
    containers = {}
    for kind, cont in CONTAINER.items():
        conts = list(root.iter(cont))
        if len(conts) != 1:
            raise ApplyError("header.xml 의 hh:%s 가 %d개(1개여야 함)" % (cont, len(conts)))
        containers[kind] = conts[0]
        for el in conts[0].kids(kind):
            defs[kind][int(el.attrs["id"])] = el
    return root, defs, containers


def _run_def_op(header_bytes, def_el, action):
    """정의 조각에 op 하나를 적용해 본다. 반환: (조각 바이트, 편집 목록)"""
    frag = header_bytes[def_el.start:def_el.end]
    froot, _ = scan(frag)
    _, fn = DEF_OPS[action["op"]]
    return frag, froot, fn(frag, froot, action)


def apply_fix_plan(src_path, out_path, inspection, check):
    actions = select_actions(check)
    paragraphs = inspection["paragraphs"]
    infos, data = _read_zip(src_path)
    header_part, sections = _paragraph_elements(src_path)
    header_bytes = data[header_part]
    _, defs, containers = _def_index(header_bytes)

    applied, not_applied = [], []
    # 1) 각 op 를 원래 정의에 시험 적용해 적용 가능 여부를 먼저 가린다
    def_actions, sec_actions, para_actions = [], [], []
    for n, a in enumerate(actions):
        a = dict(a, action_no=n + 1)
        op = a["op"]
        try:
            if op in DEF_OPS:
                kind = DEF_OPS[op][0]
                did = a.get(ID_KEY[kind])
                if did not in defs[kind]:
                    raise OpNotApplicable("header.xml 에 hh:%s id=%s 정의가 없음" % (kind, did))
                if TARGET_UNIT[kind] == "cell" and not a.get("cells"):
                    raise OpNotApplicable("대상 셀이 지정되지 않음")
                if TARGET_UNIT[kind] == "paragraph" and not a.get("paragraphs"):
                    raise OpNotApplicable("대상 문단이 지정되지 않음")
                _run_def_op(header_bytes, defs[kind][did], a)
                def_actions.append((kind, did, a))
            elif op in SECTION_OPS or op == "add_page_number":
                sec_actions.append(a)
            elif op in PARAGRAPH_OPS:
                if not a.get("paragraphs"):
                    raise OpNotApplicable("대상 문단이 지정되지 않음")
                para_actions.append(a)
            else:
                raise OpNotApplicable("지원하지 않는 수정 동작: %s" % op)
        except (OpNotApplicable, XmlPatchError) as e:
            not_applied.append(dict(a, reason=str(e)))

    # 2) 정의별로 대상 문단을 '적용할 op 묶음' 이 같은 것끼리 나눈다
    # 대상 키: 문단은 index(int), 셀은 ("cell", 표 id, 행, 열)
    per_target = defaultdict(list)  # (kind, def_id, target) -> [action]
    for kind, did, a in def_actions:
        if TARGET_UNIT[kind] == "cell":
            for c in a["cells"]:
                per_target[(kind, did, ("cell", c["table_id"], c["row"], c["col"]))].append(a)
        else:
            for pi in a["paragraphs"]:
                per_target[(kind, did, pi)].append(a)
    groups = OrderedDict()  # (kind, def_id, op 묶음) -> [target]
    for (kind, did, tg), acts in sorted(per_target.items(), key=lambda kv: (kv[0][0], kv[0][1], str(kv[0][2]))):
        key = (kind, did, tuple(sorted(x["action_no"] for x in acts)))
        groups.setdefault(key, []).append(tg)

    by_no = {a["action_no"]: a for _, _, a in def_actions}
    next_id = {k: (max(v) + 1 if v else 0) for k, v in defs.items()}
    new_defs = {k: [] for k in CONTAINER}
    clones = []
    retarget = defaultdict(list)  # target -> [(kind, old, new)]
    for (kind, did, nos), targets in groups.items():
        frag_edits = []
        frag, froot, _ = _run_def_op(header_bytes, defs[kind][did], by_no[nos[0]])
        for no in nos:
            _, _, edits = _run_def_op(header_bytes, defs[kind][did], by_no[no])
            frag_edits.extend(edits)
        new_id = next_id[kind]
        next_id[kind] += 1
        frag_edits.append(set_attr_edit(frag, froot, "id", new_id))
        new_defs[kind].append(apply_edits(frag, _dedupe(frag_edits)))
        paras = [t for t in targets if not isinstance(t, tuple)]
        cells = [{"table_id": t[1], "row": t[2], "col": t[3]} for t in targets if isinstance(t, tuple)]
        clones.append({"kind": kind, "from_id": did, "new_id": new_id, "paragraphs": paras, "cells": cells,
                       "action_nos": list(nos), "ops": sorted({by_no[n]["op"] for n in nos})})
        for t in targets:
            retarget[t].append((kind, did, new_id))

    # 3) header.xml: 복제 정의를 각 목록 끝에 추가하고 itemCnt 갱신
    h_edits = []
    for kind, blobs in new_defs.items():
        if not blobs:
            continue
        cont = containers[kind]
        close_at = header_bytes.rfind(b"</", cont.start, cont.end)
        h_edits.append((close_at, close_at, b"".join(blobs)))
        if "itemCnt" in cont.attrs:
            h_edits.append(set_attr_edit(header_bytes, cont, "itemCnt", int(cont.attrs["itemCnt"]) + len(blobs)))
    new_data = dict(data)
    if h_edits:
        new_data[header_part] = apply_edits(header_bytes, h_edits)

    # 4) section*.xml: 대상 문단의 참조 바꾸기 + 구역 op
    sec_edit_lists = defaultdict(list)
    sec_scans = {}

    def sec_scan(si):
        if si not in sec_scans:
            part = sections[si][0]
            root, order = scan(data[part])
            sec_scans[si] = (part, root, [e for e in order if e.local == "p"])
        return sec_scans[si]

    def para_elem(pi):
        para = paragraphs[pi]
        si = para["section"]
        part, _, p_elems = sec_scan(si)
        ordinal = sections[si][1].get(para["path"])
        if ordinal is None or ordinal >= len(p_elems):
            raise ApplyError("문단 #%d(%s) 의 XML 위치를 찾지 못함" % (pi, para["path"]))
        return si, part, p_elems[ordinal]

    tables_by_id = {t["id"]: t for t in inspection.get("tables") or []}

    def cell_elem(table_id, row, col):
        """표 셀(hp:tc) 요소: 표의 앵커 문단 → 그 문단의 n번째 hp:tbl → cellAddr 가 (row, col) 인 hp:tc."""
        t = tables_by_id.get(table_id)
        if not t:
            raise ApplyError("표 %s 를 분석 결과에서 찾지 못함" % table_id)
        si, part, p_el = para_elem(t["anchor_paragraph"])
        try:
            k = int(str(t["path"]).rsplit("/tbl", 1)[1])
        except (IndexError, ValueError):
            raise ApplyError("표 %s 의 경로(%s)에서 순번을 읽지 못함" % (table_id, t.get("path")))
        tbls = [tb for run in p_el.kids("run") for tb in run.kids("tbl")]
        if k >= len(tbls):
            raise ApplyError("문단 #%d 에 hp:tbl %d번이 없음" % (t["anchor_paragraph"], k))
        for tr in tbls[k].kids("tr"):
            for tc in tr.kids("tc"):
                addr = tc.kids("cellAddr")
                if addr and addr[0].attrs.get("rowAddr") == str(row) and addr[0].attrs.get("colAddr") == str(col):
                    return si, part, tc
        raise ApplyError("표 %s 의 셀(%d행 %d열)을 XML 에서 찾지 못함" % (table_id, row + 1, col + 1))

    for tg, items in sorted(retarget.items(), key=lambda kv: str(kv[0])):
        if isinstance(tg, tuple):
            si, part, tc_el = cell_elem(tg[1], tg[2], tg[3])
            sec_bytes = data[part]
            for kind, old, new in items:
                if tc_el.attrs.get("borderFillIDRef") != str(old):
                    raise ApplyError("표 %s 셀(%d행 %d열)의 borderFillIDRef 가 %s 가 아님" % (tg[1], tg[2] + 1, tg[3] + 1, old))
                sec_edit_lists[si].append(set_attr_edit(sec_bytes, tc_el, "borderFillIDRef", new))
            continue
        pi = tg
        si, part, p_el = para_elem(pi)
        sec_bytes = data[part]
        for kind, old, new in items:
            if kind == "paraPr":
                if p_el.attrs.get("paraPrIDRef") != str(old):
                    raise ApplyError("문단 #%d 의 paraPrIDRef 가 %s 가 아님" % (pi, old))
                sec_edit_lists[si].append(set_attr_edit(sec_bytes, p_el, "paraPrIDRef", new))
            else:
                runs = [r for r in p_el.kids("run") if r.attrs.get("charPrIDRef") == str(old)]
                if not runs:
                    raise ApplyError("문단 #%d 에 charPrIDRef=%s 인 run 이 없음" % (pi, old))
                for r in runs:
                    sec_edit_lists[si].append(set_attr_edit(sec_bytes, r, "charPrIDRef", new))

    added_controls = []
    for a in sec_actions:
        si = a.get("section_index")
        try:
            if si is None or si >= len(sections):
                raise OpNotApplicable("구역 %s 이 없음" % si)
            part, root, _ = sec_scan(si)
            if a["op"] == "add_page_number":
                sec_edit_lists[si].extend(op_add_page_number(data[part], root, a))
                first = next((pp["index"] for pp in paragraphs if pp.get("section") == si and pp["path"] == "sec%d/p0" % si), None)
                added_controls.append({"index": first, "section": si, "control": "pageNum"})
                applied.append(dict(a))
                continue
            page_prs = [e for e in root.iter("pagePr") if e.has_ancestor("secPr")]
            if not page_prs:
                raise OpNotApplicable("구역 %d 에 hp:secPr/hp:pagePr 가 없음" % si)
            sec_edit_lists[si].extend(SECTION_OPS[a["op"]](data[part], page_prs[0], a))
            applied.append(dict(a))
        except (OpNotApplicable, XmlPatchError) as e:
            not_applied.append(dict(a, reason=str(e)))

    deleted, marker_edits = [], []
    for a in para_actions:
        try:
            for pi in a["paragraphs"]:
                si, part, p_el = para_elem(pi)
                sec_edit_lists[si].extend(PARAGRAPH_OPS[a["op"]](data[part], p_el, a))
                if a["op"] == "delete_empty_paragraph":
                    deleted.append({"index": pi, "path": paragraphs[pi]["path"], "section": si})
                elif a["op"] == "replace_marker":
                    marker_edits.append({"index": pi, "from": a["from"], "to": a["to"]})
            applied.append(dict(a))
        except (OpNotApplicable, XmlPatchError, ApplyError) as e:
            not_applied.append(dict(a, reason=str(e)))

    for si, edits in sec_edit_lists.items():
        part = sections[si][0]
        new_data[part] = apply_edits(data[part], _dedupe(edits))

    applied_nos = {n for c in clones for n in c["action_nos"]}
    applied.extend(by_no[n] for n in sorted(applied_nos))
    applied.sort(key=lambda a: a["action_no"])

    write_zip(out_path, infos, new_data)
    return OrderedDict([
        ("selected_action_count", len(actions)),
        ("applied", applied),
        ("not_applied", not_applied),
        ("clones", clones),
        ("deleted_paragraphs", deleted),
        ("marker_edits", marker_edits),
        ("added_controls", added_controls),
        ("changed_parts", sorted(k for k in new_data if new_data[k] != data[k])),
    ])


def _dedupe(edits):
    """같은 위치를 같은 값으로 바꾸는 편집은 하나로 합친다. 같은 위치에 다른 값이면 충돌."""
    seen = {}
    for s, e, b in edits:
        if (s, e) in seen and seen[(s, e)] != b:
            raise ApplyError("같은 속성에 서로 다른 값을 쓰려는 수정 충돌(위치 %d)" % s)
        seen[(s, e)] = b
    return [(s, e, b) for (s, e), b in seen.items()]

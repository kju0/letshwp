"""수정본 검증: 내용·구조가 원본과 같고, 의도한 서식만 바뀌었는지 확인한다.

하나라도 실패하면 수정본을 정상 결과로 내보내지 않는다(CLAUDE.md 절대 원칙).
"""

import zipfile
from collections import OrderedDict

PASS, FAIL = "PASS", "FAIL"
# 서식 수정으로 바뀔 수 있는 줄 배치 캐시 외에는 문단 기록이 같아야 한다
PARA_STRUCT_KEYS = ("path", "section", "context", "text", "is_empty", "objects", "controls", "auto_numbering", "list_marker")
TABLE_KEYS = ("id", "path", "anchor_paragraph", "rows", "cols")
CELL_KEYS = ("row", "col", "row_span", "col_span", "text")


def _check(name, ok, detail):
    return OrderedDict([("name", name), ("status", PASS if ok else FAIL), ("detail", detail)])


def _zip_entries(path):
    with zipfile.ZipFile(path) as z:
        return [(i.filename, i.compress_type) for i in z.infolist()], {i.filename: z.read(i.filename) for i in z.infolist()}


def verify(src_path, out_path, before_ins, after_ins, before_check, after_check, fix):
    checks = []

    # 1) 패키지: 엔트리 목록·순서·압축 방식 동일, 바꾼 파트 외에는 바이트 동일
    (e1, d1), (e2, d2) = _zip_entries(src_path), _zip_entries(out_path)
    checks.append(_check("패키지 엔트리 목록·순서·압축 방식 동일", e1 == e2,
                         "엔트리 %d개" % len(e1) if e1 == e2 else "원본 %s / 수정본 %s" % (e1, e2)))
    allowed = set(fix["changed_parts"])
    touched = sorted(n for n in d1 if d1.get(n) != d2.get(n))
    unexpected = [n for n in touched if n not in allowed]
    checks.append(_check("수정 대상 파트 외 바이트 동일", not unexpected,
                         "변경 파트: %s" % (", ".join(touched) or "없음") if not unexpected else "예상 밖 변경: %s" % unexpected))
    checks.append(_check("mimetype 첫 엔트리·무압축", bool(e2) and e2[0] == ("mimetype", zipfile.ZIP_STORED),
                         "mimetype=%r" % d2.get("mimetype")))

    # 2) Inspector 자체 검증(텍스트 대조·표 격자·미처리 요소)
    v = after_ins.get("verification") or {}
    checks.append(_check("수정본 Inspector 검증 통과", bool(v.get("all_ok")), "verification.all_ok=%s" % v.get("all_ok")))

    # 3) 문단 수·순서·텍스트·개체 동일
    p1, p2 = before_ins["paragraphs"], after_ins["paragraphs"]
    diffs = []
    if len(p1) != len(p2):
        diffs.append("문단 수 %d → %d" % (len(p1), len(p2)))
    else:
        for a, b in zip(p1, p2):
            for k in PARA_STRUCT_KEYS:
                if a.get(k) != b.get(k):
                    diffs.append("문단 #%d %s" % (a["index"], k))
    checks.append(_check("문단 수·순서·텍스트·개체 동일", not diffs,
                         "문단 %d개 일치" % len(p1) if not diffs else "; ".join(diffs[:10])))

    # 4) 표 구조(행·열·셀 병합·셀 텍스트), 그림 동일
    t_diffs = []
    t1, t2 = before_ins.get("tables") or [], after_ins.get("tables") or []
    if len(t1) != len(t2):
        t_diffs.append("표 수 %d → %d" % (len(t1), len(t2)))
    else:
        for a, b in zip(t1, t2):
            if any(a.get(k) != b.get(k) for k in TABLE_KEYS):
                t_diffs.append("표 %s 속성" % a.get("id"))
            ca = [tuple(c.get(k) for k in CELL_KEYS) for c in a.get("cells") or []]
            cb = [tuple(c.get(k) for k in CELL_KEYS) for c in b.get("cells") or []]
            if ca != cb:
                t_diffs.append("표 %s 셀" % a.get("id"))
    pics1 = [(p.get("path"), p.get("binary_path")) for p in before_ins.get("pictures") or []]
    pics2 = [(p.get("path"), p.get("binary_path")) for p in after_ins.get("pictures") or []]
    if pics1 != pics2:
        t_diffs.append("그림 %d → %d" % (len(pics1), len(pics2)))
    checks.append(_check("표 구조·셀 병합·그림 동일", not t_diffs,
                         "표 %d개, 그림 %d개 일치" % (len(t1), len(pics1)) if not t_diffs else "; ".join(t_diffs)))

    # 5) 대상이 아닌 문단의 서식은 그대로
    targets = {pi for c in fix["clones"] for pi in c.get("paragraphs") or []}
    f_diffs = []
    if len(p1) == len(p2):
        for a, b in zip(p1, p2):
            if a["index"] in targets:
                continue
            if a.get("para_format") != b.get("para_format") or a.get("runs") != b.get("runs") \
                    or a.get("char_format") != b.get("char_format"):
                f_diffs.append("#%d" % a["index"])
    checks.append(_check("수정 대상이 아닌 문단의 서식 변화 없음", not f_diffs,
                         "대상 문단 %d개 외 변화 없음" % len(targets) if not f_diffs else "변화: %s" % ", ".join(f_diffs[:20])))

    # 5-2) 대상이 아닌 표 셀의 테두리 정의는 그대로
    cell_targets = {(x["table_id"], x["row"], x["col"]) for c in fix["clones"] for x in c.get("cells") or []}
    c_diffs = []
    if len(t1) == len(t2):
        for a, b in zip(t1, t2):
            for ca, cb in zip(a.get("cells") or [], b.get("cells") or []):
                if (a.get("id"), ca.get("row"), ca.get("col")) in cell_targets:
                    continue
                if ca.get("border_fill_id") != cb.get("border_fill_id") or ca.get("borders") != cb.get("borders"):
                    c_diffs.append("%s %s행 %s열" % (a.get("id"), (ca.get("row") or 0) + 1, (ca.get("col") or 0) + 1))
    checks.append(_check("수정 대상이 아닌 표 셀의 테두리 변화 없음", not c_diffs,
                         "대상 셀 %d개 외 변화 없음" % len(cell_targets) if not c_diffs else "변화: %s" % ", ".join(c_diffs[:20])))

    # 6) 재검사: 적용한 수정의 FAIL 이 PASS 로 바뀌었고, 원래 PASS 였던 항목이 FAIL 이 되지 않음
    def keyed(check):
        out = {}
        for r in check["results"]:
            t = r.get("target") or {}
            if t.get("path") or t.get("type") == "section":
                out[(r["rule_id"], t.get("path"), t.get("section_index"))] = r
        return out
    kb, ka = keyed(before_check), keyed(after_check)
    by_id = {r["id"]: r for r in before_check["results"]}
    applied_results = {rid for a in fix["applied"] for rid in a.get("result_ids") or []}
    blocked_results = {rid for a in fix["not_applied"] for rid in a.get("result_ids") or []}
    still_fail = []
    for rid in sorted(applied_results - blocked_results):
        r = by_id.get(rid)
        if not r:
            continue
        t = r.get("target") or {}
        after = ka.get((r["rule_id"], t.get("path"), t.get("section_index")))
        if after is None or after["status"] != PASS:
            still_fail.append("%s(%s → %s)" % (rid, r["status"], after["status"] if after else "없음"))
    checks.append(_check("적용한 수정 항목이 재검사에서 적합(PASS)", not still_fail,
                         "%d건 모두 PASS" % len(applied_results - blocked_results) if not still_fail else "; ".join(still_fail[:10])))
    regress = ["%s %s" % (k[0], k[1] or k[2]) for k, r in kb.items()
               if r["status"] == PASS and k in ka and ka[k]["status"] == FAIL]
    checks.append(_check("원래 적합(PASS)이던 항목이 부적합(FAIL)으로 바뀌지 않음", not regress,
                         "회귀 없음" if not regress else "; ".join(regress[:10])))

    return OrderedDict([("all_ok", all(c["status"] == PASS for c in checks)), ("checks", checks)])

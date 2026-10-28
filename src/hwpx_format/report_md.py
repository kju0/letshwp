"""수정 결과를 사람이 읽는 마크다운 보고서로 만든다."""

from collections import OrderedDict

from hwpx_check.report import location, OP_LABEL

STATUSES = (("PASS", "적합"), ("FAIL", "수정 필요"), ("UNKNOWN", "판단 불가"), ("SKIP", "검사 제외"))
KIND_LABEL = {"charPr": "글자 모양", "paraPr": "문단 모양"}


def _cell(v):
    s = "-" if v is None or v == "" else str(v)
    return s.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return out


def _disp(v):
    return (v or {}).get("display")


def _excerpt(r):
    return (r.get("target") or {}).get("text_excerpt")


def render_markdown(meta, before, after, fix, verification):
    ok = verification["all_ok"]
    L = ["# 서식 수정 보고서", ""]
    if ok:
        L.append("> ✅ **수정 완료 · 검증 통과** — 수정본: `%s`" % meta["output_name"])
    else:
        L.append("> ❌ **검증 실패 — 수정본을 만들지 않았습니다.** 아래 3절의 실패 항목을 확인하세요.")
    L.append("")
    L += _table(["항목", "내용"], [
        ["원본 문서", meta["source_name"]],
        ["수정본", meta["output_name"] if ok else "(생성 안 함)"],
        ["처리 시각", meta.get("run_at")],
        ["코드 버전", meta.get("code_version")],
        ["표준 기준", (before.get("checker") or {}).get("spec_version")],
        ["문서 유형", ((before.get("document") or {}).get("document_type") or {}).get("value") or "미지정"],
        ["원본 변경", "없음 (SHA-256 동일)" if meta.get("source_unchanged") else "**변경됨 — 확인 필요**"],
    ])

    # 1. 요약
    sb, sa = before["summary"], after["summary"] if after else None
    applied_ids = _applied_result_ids(fix)
    L += ["", "## 1. 한눈에 보기", ""]
    L += _table(["구분", "수정 전", "수정 후"],
                [[label, sb[k], sa[k] if sa else "-"] for k, label in STATUSES] + [["합계", sb["total"], sa["total"] if sa else "-"]])
    para_count = len({pi for c in fix["clones"] for pi in c.get("paragraphs") or []})
    cell_count = len({(x["table_id"], x["row"], x["col"]) for c in fix["clones"] for x in c.get("cells") or []})
    L += ["",
          "- 수정한 항목: **%d건** (문단 %d개%s, 서식 정의 %d개 추가)" % (
              len(applied_ids), para_count, ", 표 셀 %d개" % cell_count if cell_count else "", len(fix["clones"])),
          "- 수정하지 않은 항목: **%d건** (아래 4절)" % (len(before["fix_plan"]["manual_review"]) + len(fix["not_applied"]))]

    # 2. 수정 내역
    L += ["", "## 2. 수정한 내용" + ("" if ok else " (검증 실패로 폐기됨)"), ""]
    by_rule = OrderedDict()
    for r in before["results"]:
        if r["id"] in applied_ids:
            by_rule.setdefault(r["rule_name"], []).append(r)
    if not by_rule:
        L.append("수정한 항목이 없습니다.")
    for name, rs in by_rule.items():
        L += ["### %s — %d건" % (name, len(rs)), ""]
        L += _table(["위치", "내용", "변경 전", "변경 후"],
                    [[location(r["target"]), _excerpt(r), _disp(r.get("current")), _disp(r.get("expected"))] for r in rs])
        L.append("")

    # 3. 검증
    L += ["## 3. 검증 결과", ""]
    L += _table(["검증 항목", "결과", "상세"],
                [[c["name"], "✅ 통과" if c["status"] == "PASS" else "❌ 실패", c["detail"]] for c in verification["checks"]])

    # 4. 수정하지 않은 항목
    L += ["", "## 4. 수정하지 않은 항목 (직접 확인 필요)", ""]
    manual = before["fix_plan"]["manual_review"]
    if manual:
        L += ["### 검사만 하는 항목 — 본문 글자를 바꿔야 하므로 자동 수정하지 않음", ""]
        L += _table(["항목", "위치", "내용", "현재", "표준"],
                    [[m["rule_name"], location(m["target"]), (m["target"] or {}).get("text_excerpt"), m["current"], m["expected"]]
                     for m in manual])
        L.append("")
    if fix["not_applied"]:
        L += ["### 적용하지 못한 수정", ""]
        L += _table(["동작", "대상", "문단", "사유"],
                    [[OP_LABEL.get(a["op"], a["op"]), _def_label(a),
                      ", ".join("#%d" % p for p in a.get("paragraphs") or []) or
                      ", ".join("%s %d행%d열" % (c["table_id"], c["row"] + 1, c["col"] + 1) for c in a.get("cells") or []), a["reason"]]
                     for a in fix["not_applied"]])
        L.append("")
    if not manual and not fix["not_applied"]:
        L += ["없습니다.", ""]
    L.append("- 판단 불가 %d건·검사 제외 %d건은 기준이 정해지지 않았거나 역할이 불확실해 수정하지 않았습니다(검사 보고서 ④절)."
             % (sb["UNKNOWN"], sb["SKIP"]))

    # 5. 항목별 비교
    if sa:
        L += ["", "## 5. 항목별 재검사 비교", ""]
        rows = []
        for rid, b in sb["by_rule"].items():
            a = sa["by_rule"].get(rid) or {}
            rows.append([b["name"], "%d → %d" % (b["FAIL"], a.get("FAIL", 0)), "%d → %d" % (b["PASS"], a.get("PASS", 0)),
                         "%d → %d" % (b["UNKNOWN"], a.get("UNKNOWN", 0))])
        L += _table(["검사 항목", "수정 필요", "적합", "판단 불가"], rows)

    L += ["", "## 6. 참고", "",
          "- 원본 파일은 열기만 했고, 수정은 복사본에만 적용했습니다.",
          "- 글자·숫자·문단 순서·표·그림은 바꾸지 않았고, 서식 속성(글자 모양·문단 모양·용지)만 바꿨습니다.",
          "- 다른 문단과 함께 쓰는 서식은 복제해서 수정 대상 문단만 새 서식을 쓰게 했습니다. 다른 문단의 서식은 그대로입니다.",
          "- 어절 단위 줄 나눔은 문단 모양의 줄 나눔 기준(한글 단위 어절)으로 설정했습니다. 줄 나눔 위치와 양쪽 정렬의 공백 조정은 한글이 문서를 열 때 다시 계산합니다.",
          "- 줄 나눔 기준·줄 간격·문단 간격이 바뀌면 줄 나눔과 쪽 나눔이 달라질 수 있습니다. 한글에서 열어 화면 배치를 확인하세요.",
          ""]
    return "\n".join(L)


def _def_label(a):
    if "char_pr_id" in a:
        return "글자 모양 id %s" % a["char_pr_id"]
    if "para_pr_id" in a:
        return "문단 모양 id %s" % a["para_pr_id"]
    if "border_fill_id" in a:
        return "테두리 정의 id %s" % a["border_fill_id"]
    return "구역 %s" % a.get("section_index")


def _applied_result_ids(fix):
    """모든 수정 동작이 적용된 검사 결과 id."""
    done = {rid for a in fix["applied"] for rid in a.get("result_ids") or []}
    blocked = {rid for a in fix["not_applied"] for rid in a.get("result_ids") or []}
    return done - blocked

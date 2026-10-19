"""HWPX 표준 서식 검사기 (원본 수정 없음).

사용법
    python3 src/checker.py output/문서.json  -o output/문서_check.json --report output/문서_check.txt
    python3 src/checker.py samples/문서.hwpx -o output/문서_check.json --report output/문서_check.txt

입력이 .json 이면 Inspector 결과로 간주하고, .hwpx 이면 Inspector 를 내부에서 실행한다.
표준값은 config/format_standard.json 에서 읽는다(--config 로 변경 가능).
"""

import argparse
import datetime
import json
import os
import sys
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hwpx_check import CHECKER_VERSION  # noqa: E402
from hwpx_check.rules import Doc, RULES, PASS, FAIL, UNKNOWN, SKIP  # noqa: E402

SCHEMA_VERSION = "1.0"
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "format_standard.json"
STATUSES = (PASS, FAIL, UNKNOWN, SKIP)

PRESERVATION_RULES = [
    "원본 HWPX 는 수정하지 않고 복제본에만 적용한다.",
    "문단 텍스트와 숫자는 변경하지 않는다(날짜·시간·번호·기호는 자동 수정 대상에서 제외).",
    "문단 순서, 표 구조(행·열·셀 병합), 그림 등 논리 구조를 변경하지 않는다.",
    "아래 actions 에 나열된 서식 속성(header.xml 의 charPr/paraPr, section 의 pagePr)만 변경한다.",
    "공유되는 charPr/paraPr 를 바꿀 때 다른 문단에 영향이 가면(clone_required) 정의를 복제해 대상 문단만 다시 참조시킨다.",
    "수정 후 Inspector 를 다시 실행해 문단 수·표 수·텍스트·표 격자 검증이 원본과 동일한지 확인하고, 실패하면 결과를 폐기한다.",
]


class CheckerError(RuntimeError):
    pass


def load_config(path=None):
    p = Path(path) if path else DEFAULT_CONFIG
    with open(p, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [rid for rid, _ in RULES if rid not in cfg.get("rules", {})]
    if missing:
        raise CheckerError("설정 파일에 규칙 정의가 없습니다: %s" % missing)
    return cfg, str(p)


def load_inspection(path):
    """입력이 Inspector JSON 이면 읽고, HWPX 이면 Inspector 를 실행한다.
    반환: (inspection dict, input_mode, 원본 HWPX 해시 검증 정보)"""
    p = Path(path)
    if not p.is_file():
        raise CheckerError("입력 파일이 없습니다: %s" % p)
    suffix = p.suffix.lower()
    if suffix == ".json":
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        for key in ("schema_version", "paragraphs", "format_definitions", "verification"):
            if key not in data:
                raise CheckerError("Inspector 결과 JSON 이 아닙니다(키 '%s' 없음): %s" % (key, p))
        return data, "inspector_json", None
    if suffix == ".hwpx":
        from inspector import inspect_hwpx
        from hwpx_analysis.package import sha256_of
        before = sha256_of(p)
        data = inspect_hwpx(p)
        after = sha256_of(p)
        if before != after:
            raise CheckerError("검사 중 원본 HWPX 가 변경되었습니다.")
        return data, "hwpx", {"sha256_before": before, "sha256_after": after, "unchanged": True}
    raise CheckerError("지원하지 않는 입력 형식입니다(.json 또는 .hwpx): %s" % p)


def check_document(inspection, config, config_path=None, input_mode="inspector_json", source_check=None):
    ver = inspection.get("verification") or {}
    if not ver.get("all_ok"):
        # CLAUDE.md: 검증에 실패하면 결과를 정상 처리하지 않는다.
        raise CheckerError("Inspector 검증이 통과하지 않은 분석 결과는 검사하지 않습니다(verification.all_ok=false).")

    doc = Doc(inspection, config)
    results = []
    for rule_id, fn in RULES:
        for n, r in enumerate(fn(doc)):
            r["id"] = "%s-%03d" % (rule_id, n + 1)
            results.append(r)

    src = inspection.get("source") or {}
    return OrderedDict([
        ("schema_version", SCHEMA_VERSION),
        ("checker", {
            "name": "letshwp-checker", "version": CHECKER_VERSION,
            "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "config_path": config_path, "spec_version": config.get("spec_version"),
            "role_confidence_min": doc.role_min,
        }),
        ("document", {
            "file_name": src.get("file_name"), "path": src.get("path"), "sha256": src.get("sha256"),
            "input_mode": input_mode,
            "inspection_generated_at": (inspection.get("analyzer") or {}).get("generated_at"),
            "analyzer_version": (inspection.get("analyzer") or {}).get("version"),
            "source_unchanged": source_check["unchanged"] if source_check else src.get("unchanged_after_analysis"),
            "paragraph_count": len(doc.paragraphs),
            "checked_paragraph_count": len(doc.text_paragraphs()),
        }),
        ("summary", summarize(results)),
        ("results", results),
        ("fix_plan", build_fix_plan(doc, results)),
        ("notes", [
            "역할(role)은 Inspector 의 규칙 기반 추정값이며, 서식값(current)은 HWPX XML 에서 추출한 실제 값이다.",
            "위치는 Inspector 문단 인덱스(#, 0부터, 표·머리말 포함 문서 순서)와 XML 경로로 표시한다. 쪽 번호는 산출하지 않는다.",
            "'분석만' 항목은 불일치가 있어도 FAIL 이 아니라 UNKNOWN(observation)으로 표시한다.",
        ]),
    ])


def summarize(results):
    total = Counter(r["status"] for r in results)
    by_rule = OrderedDict()
    for r in results:
        b = by_rule.setdefault(r["rule_id"], {"name": r["rule_name"], "spec_mode": r["spec_mode"],
                                              **{s: 0 for s in STATUSES}})
        b[r["status"]] += 1
    return {"total": len(results), **{s: total.get(s, 0) for s in STATUSES}, "by_rule": by_rule}


def _users(doc):
    """charPr/paraPr id -> 참조하는 문단 인덱스 집합(공백만 있는 run 포함)."""
    char_users, para_users = defaultdict(set), defaultdict(set)
    for p in doc.paragraphs:
        for run in p.get("runs") or []:
            if run.get("char_pr_id") is not None:
                char_users[run["char_pr_id"]].add(p["index"])
        pid = (p.get("para_format") or {}).get("para_pr_id")
        if pid is not None:
            para_users[pid].add(p["index"])
    return char_users, para_users


def build_fix_plan(doc, results):
    """FAIL 결과의 수정 동작을 (동작, 대상 정의, 목표값) 단위로 묶어 Formatter 입력 형태로 만든다."""
    char_users, para_users = _users(doc)
    grouped = OrderedDict()
    manual = []
    for r in results:
        if r["status"] != FAIL:
            continue
        if not r["fix"]["possible"]:
            manual.append({"result_id": r["id"], "rule_id": r["rule_id"], "rule_name": r["rule_name"],
                           "target": r["target"], "current": (r.get("current") or {}).get("display"),
                           "expected": (r.get("expected") or {}).get("display"),
                           "recommendation": r["fix"]["recommendation"]})
            continue
        for a in r["fix"]["actions"]:
            ref = ("char_pr_id", a["char_pr_id"]) if "char_pr_id" in a else \
                  ("para_pr_id", a["para_pr_id"]) if "para_pr_id" in a else ("section_index", a.get("section_index"))
            key = (a["op"], ref, json.dumps(a.get("to"), ensure_ascii=False, sort_keys=True), json.dumps(a.get("langs") or a.get("lang")))
            g = grouped.get(key)
            if g is None:
                g = dict(a, rule_id=r["rule_id"], mode=r["fix"]["mode"], result_ids=[], paragraphs=[])
                g.pop("paragraph_index", None)
                grouped[key] = g
            g["result_ids"].append(r["id"])
            if r["target"].get("paragraph_index") is not None:
                g["paragraphs"].append(r["target"]["paragraph_index"])
    actions = []
    for (op, ref, _, _), g in grouped.items():
        users = char_users.get(ref[1], set()) if ref[0] == "char_pr_id" else \
            para_users.get(ref[1], set()) if ref[0] == "para_pr_id" else set()
        others = sorted(users - set(g["paragraphs"]))
        if ref[0] in ("char_pr_id", "para_pr_id"):
            g["shared_by_other_paragraphs"] = others
            g["clone_required"] = bool(others)
        actions.append(g)
    return OrderedDict([
        ("preservation_rules", PRESERVATION_RULES),
        ("auto_actions", [a for a in actions if a["mode"] == "auto"]),
        ("role_based_actions", [a for a in actions if a["mode"] == "auto_if_role_confirmed"]),
        ("manual_review", manual),
        ("not_planned", "PASS·UNKNOWN·SKIP 항목과 '분석만' 항목은 수정 계획에 포함하지 않는다."),
    ])


def _assert_safe_output(out_path, source_path=None):
    out = Path(out_path).resolve()
    if out.suffix.lower() == ".hwpx":
        raise CheckerError("결과 파일 확장자로 .hwpx 를 사용할 수 없습니다(원본 보호): %s" % out)
    if source_path and out == Path(source_path).resolve():
        raise CheckerError("결과 경로가 원본 경로와 같습니다.")
    return out


def save_json(result, out_path):
    out = _assert_safe_output(out_path, (result.get("document") or {}).get("path"))
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return out


def save_report(result, out_path):
    from hwpx_check.report import render_report
    out = _assert_safe_output(out_path, (result.get("document") or {}).get("path"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report(result), encoding="utf-8")
    return out


def run(input_path, output=None, report=None, config_path=None):
    config, cpath = load_config(config_path)
    inspection, mode, src_check = load_inspection(input_path)
    result = check_document(inspection, config, cpath, mode, src_check)
    stem = Path(input_path).stem
    if mode == "inspector_json" and stem.endswith("-output"):
        stem = stem[: -len("-output")]
    out = save_json(result, output or os.path.join("output", stem + "_check.json"))
    rp = save_report(result, report) if report else None
    return result, out, rp


def main(argv=None):
    ap = argparse.ArgumentParser(description="HWPX 표준 서식 검사 (원본 수정 없음)")
    ap.add_argument("input", help="Inspector 결과 .json 또는 .hwpx")
    ap.add_argument("-o", "--output", help="검사 결과 JSON 경로 (기본: output/<이름>_check.json)")
    ap.add_argument("--report", help="TXT 보고서 경로(선택)")
    ap.add_argument("--config", help="표준값 설정 파일 (기본: config/format_standard.json)")
    args = ap.parse_args(argv)
    try:
        result, out, rp = run(args.input, args.output, args.report, args.config)
    except CheckerError as e:
        print("오류: %s" % e, file=sys.stderr)
        return 2
    s = result["summary"]
    print("검사 결과 JSON: %s" % out)
    if rp:
        print("검사 보고서   : %s" % rp)
    print("검사 %d건 - 적합 %d / 수정 필요 %d / 판단 불가 %d / 검사 제외 %d" % (
        s["total"], s[PASS], s[FAIL], s[UNKNOWN], s[SKIP]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

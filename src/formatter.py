"""HWPX 표준 서식 수정기 (원본 수정 없음).

검사(Checker) 결과의 수정 계획(fix_plan)을 HWPX 복제본에 적용하고, 수정본을 다시 분석·검사해 검증한다.
검증에 실패하면 수정본을 지우고 오류로 끝난다.

사용법
    python3 src/formatter.py samples/문서.hwpx --out-dir output/문서_fix [--doc-type A|B]

결과 폴더
    <이름>_수정본.hwpx       수정본(검증 통과 시에만)
    change_report.md         수정 내역 마크다운 보고서
    fix_result.json          적용/미적용 동작, 서식 복제 내역, 검증 결과
    check_before.json/.txt   수정 전 검사 결과
    check_after.json/.txt    수정본 검사 결과
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import checker  # noqa: E402
from inspector import inspect_hwpx, InspectionError  # noqa: E402
from hwpx_analysis.package import sha256_of  # noqa: E402
from hwpx_format import FORMATTER_VERSION  # noqa: E402
from hwpx_format.apply import apply_fix_plan, ApplyError  # noqa: E402
from hwpx_format.verify import verify  # noqa: E402
from hwpx_format.report_md import render_markdown  # noqa: E402
from hwpx_format.xmlpatch import XmlPatchError  # noqa: E402


class FormatterError(RuntimeError):
    pass


def output_name(src):
    return "%s_수정본.hwpx" % Path(src).stem


def _check(inspection, config, cpath, doc_type, mode="hwpx"):
    return checker.check_document(inspection, config, cpath, mode, None, doc_type)


def format_hwpx(src_path, out_dir, doc_type=None, config_path=None, meta=None, source_name=None):
    """src_path 를 수정해 out_dir 에 결과를 쓴다. 반환: fix_result dict (ok 여부 포함).
    source_name: 보고서·수정본 이름에 쓸 원본 파일명(복사본을 입력으로 줄 때 원래 이름 유지용)."""
    src = Path(src_path).resolve()
    out_dir = Path(out_dir).resolve()
    if not src.is_file() or src.suffix.lower() != ".hwpx":
        raise FormatterError("hwpx 파일이 아닙니다: %s" % src)
    out_dir.mkdir(parents=True, exist_ok=True)
    source_name = source_name or src.name
    out_hwpx = out_dir / output_name(source_name)
    if out_hwpx == src:
        raise FormatterError("수정본 경로가 원본과 같습니다.")
    sha_before = sha256_of(src)
    config, cpath = checker.load_config(config_path)

    errors = []
    try:
        before_ins = inspect_hwpx(str(src))
        before = _check(before_ins, config, cpath, doc_type)
    except (InspectionError, checker.CheckerError) as e:
        raise FormatterError("수정 전 검사 실패: %s" % e)
    checker.save_json(before, out_dir / "check_before.json")
    checker.save_report(before, out_dir / "check_before_report.txt")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_out = Path(tmp) / "formatted.hwpx"
        try:
            fix = apply_fix_plan(str(src), str(tmp_out), before_ins, before)
        except (ApplyError, XmlPatchError) as e:
            raise FormatterError("수정 적용 실패: %s" % e)
        after, after_ins = None, None
        try:
            after_ins = inspect_hwpx(str(tmp_out))
            after = _check(after_ins, config, cpath, doc_type)
        except (InspectionError, checker.CheckerError) as e:
            errors.append("수정본 재검사 실패: %s" % e)
        if after is not None:
            verification = verify(str(src), str(tmp_out), before_ins, after_ins, before, after, fix)
            if not verification["all_ok"]:
                errors.append("수정본 검증 실패: " + "; ".join(
                    "%s(%s)" % (c["name"], c["detail"]) for c in verification["checks"] if c["status"] != "PASS"))
        else:
            verification = {"all_ok": False, "checks": [{"name": "수정본 재검사", "status": "FAIL", "detail": errors[-1]}]}
        if not errors:
            shutil.copyfile(tmp_out, out_hwpx)
    if errors and out_hwpx.exists():
        out_hwpx.unlink()

    if after is not None:
        checker.save_json(after, out_dir / "check_after.json")
        checker.save_report(after, out_dir / "check_after_report.txt")

    sha_after = sha256_of(src)
    if sha_after != sha_before:
        errors.append("원본 파일 해시가 실행 중 바뀌었습니다.")
    ok = not errors
    meta = dict(meta or {}, source_name=source_name, output_name=out_hwpx.name, source_unchanged=sha_before == sha_after)
    (out_dir / "change_report.md").write_text(
        render_markdown(meta, before, after, fix, dict(verification, all_ok=verification["all_ok"] and ok)), encoding="utf-8")

    result = OrderedDict([
        ("formatter_version", FORMATTER_VERSION),
        ("ok", ok),
        ("errors", errors),
        ("source", {"path": str(src), "sha256_before": sha_before, "sha256_after": sha_after,
                    "unchanged": sha_before == sha_after}),
        ("output", str(out_hwpx) if ok else None),
        ("summary_before", {k: v for k, v in before["summary"].items() if k != "by_rule"}),
        ("summary_after", {k: v for k, v in after["summary"].items() if k != "by_rule"} if after else None),
        ("fix", fix),
        ("verification", verification),
    ])
    with open(out_dir / "fix_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="HWPX 표준 서식 수정 (원본 수정 없음, 수정본 검증)")
    ap.add_argument("input", help="수정할 .hwpx")
    ap.add_argument("--out-dir", required=True, help="결과 폴더")
    ap.add_argument("--doc-type", choices=["A", "B"], help="문서 유형(TYPE A/B 간격 특례)")
    ap.add_argument("--config", help="표준값 설정 파일 (기본: config/format_standard.json)")
    args = ap.parse_args(argv)
    try:
        res = format_hwpx(args.input, args.out_dir, args.doc_type, args.config)
    except FormatterError as e:
        print("오류: %s" % e, file=sys.stderr)
        return 2
    print("수정본: %s" % (res["output"] or "생성 안 함"))
    for e in res["errors"]:
        print("오류: %s" % e, file=sys.stderr)
    return 0 if res["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())

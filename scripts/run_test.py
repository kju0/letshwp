"""테스트 모드 실행기: hwpx 하나를 Inspector → Checker 로 실행하고 결과 파일을 한 폴더에 모은다.

사용
    python3 scripts/run_test.py <파일.hwpx> [--doc-type A|B] [--out-root output]

결과 폴더: <out-root>/<파일명>_<YYYYMMDD-HHMMSS>/
    source.hwpx              원본 복사본(원본은 열기만 하고 수정하지 않는다)
    inspection.json          Inspector 결과
    inspection_report.txt    Inspector 텍스트 보고서
    check.json               Checker 결과
    check_report.txt         Checker 텍스트 보고서(①~⑤)
    run_info.json            실행 정보(원본 SHA-256 전후, git 커밋, 시각, 명령, 결과 요약)

원본 해시가 바뀌었거나 Inspector 검증이 실패하면 종료 코드 2 로 끝나며 정상 결과로 취급하지 않는다.
"""

import argparse
import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from inspector import inspect_hwpx, save_json as save_inspection  # noqa: E402
from hwpx_analysis.package import sha256_of  # noqa: E402
from hwpx_analysis.report import render_report as render_inspection  # noqa: E402
import checker  # noqa: E402


class RunTestError(RuntimeError):
    pass


def git_info():
    def g(*args):
        try:
            return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                                  timeout=10).stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    status = g("status", "--porcelain", "--", "src", "config", "scripts")
    return {"commit": g("rev-parse", "HEAD"), "branch": g("rev-parse", "--abbrev-ref", "HEAD"),
            "uncommitted_code_changes": bool(status)}


def run_test(hwpx_path, doc_type=None, out_root=None, config_path=None, now=None):
    src = Path(hwpx_path).resolve()
    if not src.is_file() or src.suffix.lower() != ".hwpx":
        raise RunTestError("hwpx 파일이 아닙니다: %s" % src)
    now = now or datetime.datetime.now()
    out_dir = Path(out_root or ROOT / "output").resolve() / ("%s_%s" % (src.stem, now.strftime("%Y%m%d-%H%M%S")))
    out_dir.mkdir(parents=True, exist_ok=False)

    sha_before = sha256_of(src)
    work = out_dir / "source.hwpx"
    shutil.copy2(src, work)
    if sha256_of(work) != sha_before:
        raise RunTestError("원본 복사본의 해시가 원본과 다릅니다.")

    # 모든 분석은 복사본으로 한다
    inspection = inspect_hwpx(str(work))
    save_inspection(inspection, out_dir / "inspection.json")
    (out_dir / "inspection_report.txt").write_text(render_inspection(inspection), encoding="utf-8")

    errors = []
    if not inspection["verification"]["all_ok"]:
        errors.append("Inspector 검증 실패(inspection.json 의 verification 확인)")

    check = None
    if not errors:
        try:
            check, _, _ = checker.run(str(out_dir / "inspection.json"), output=str(out_dir / "check.json"),
                                      report=str(out_dir / "check_report.txt"),
                                      config_path=config_path, doc_type=doc_type)
        except checker.CheckerError as e:
            errors.append("Checker 오류: %s" % e)

    sha_after = sha256_of(src)
    if sha_after != sha_before:
        errors.append("원본 파일 해시가 실행 중 바뀌었습니다.")

    info = {
        "input": str(src),
        "source_sha256_before": sha_before,
        "source_sha256_after": sha_after,
        "source_unchanged": sha_before == sha_after,
        "run_at": now.isoformat(timespec="seconds"),
        "command": "python3 scripts/run_test.py %s%s" % (src.name, " --doc-type " + doc_type if doc_type else ""),
        "doc_type": doc_type,
        "code": git_info(),
        "ok": not errors,
        "errors": errors,
        "check_summary": ({k: v for k, v in check["summary"].items() if k != "by_rule"} if check else None),
        "files": sorted(p.name for p in out_dir.iterdir()) + ["run_info.json"],
    }
    with open(out_dir / "run_info.json", "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    return out_dir, info


def main(argv=None):
    ap = argparse.ArgumentParser(description="hwpx 를 Inspector → Checker 로 실행하고 결과를 한 폴더에 저장")
    ap.add_argument("input", help="검사할 .hwpx 파일")
    ap.add_argument("--doc-type", choices=["A", "B"], help="문서 유형(지정 시 TYPE A/B 특수 간격 검사)")
    ap.add_argument("--out-root", help="결과 상위 폴더 (기본: output/)")
    ap.add_argument("--config", help="표준값 설정 파일 (기본: config/format_standard.json)")
    args = ap.parse_args(argv)
    try:
        out_dir, info = run_test(args.input, args.doc_type, args.out_root, args.config)
    except RunTestError as e:
        print("실패: %s" % e)
        return 2
    print("결과 폴더: %s" % out_dir)
    for name in info["files"]:
        print("  - %s" % name)
    print("원본 변경 없음: %s" % ("예" if info["source_unchanged"] else "아니오"))
    print("코드 버전: %s%s" % ((info["code"]["commit"] or "?")[:7],
                              " (커밋 안 된 코드 변경 포함)" if info["code"]["uncommitted_code_changes"] else ""))
    if info["check_summary"]:
        s = info["check_summary"]
        print("검사: 총 %d건 — PASS %d, FAIL %d, UNKNOWN %d, SKIP %d"
              % (s["total"], s["PASS"], s["FAIL"], s["UNKNOWN"], s["SKIP"]))
    if not info["ok"]:
        for e in info["errors"]:
            print("오류: %s" % e)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

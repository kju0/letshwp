"""수정 모드 실행기: hwpx 하나를 검사 → 서식 수정 → 재검사·검증하고 결과 파일을 한 폴더에 모은다.

사용
    python3 scripts/run_fix.py <파일.hwpx> [--doc-type A|B] [--out-root output]

결과 폴더: <out-root>/<파일명>_<YYYYMMDD-HHMMSS>_수정/
    source.hwpx                원본 복사본(원본은 열기만 하고 수정하지 않는다)
    <파일명>_수정본.hwpx       서식을 수정한 문서(검증 통과 시에만 생성)
    change_report.md           수정 내역 마크다운 보고서
    fix_result.json            적용/미적용 동작, 서식 복제 내역, 검증 결과
    check_before.json / check_before_report.txt   수정 전 검사
    check_after.json  / check_after_report.txt    수정본 검사
    run_info.json              실행 정보(원본 SHA-256 전후, git 커밋, 시각, 명령, 결과 요약)

수정본 검증이 실패하거나 원본 해시가 바뀌면 종료 코드 2 로 끝나며, 수정본은 만들지 않는다.
"""

import argparse
import datetime
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from hwpx_analysis.package import sha256_of  # noqa: E402
from formatter import format_hwpx, FormatterError  # noqa: E402
from run_test import git_info  # noqa: E402


class RunFixError(RuntimeError):
    pass


def run_fix(hwpx_path, doc_type=None, out_root=None, config_path=None, now=None):
    src = Path(hwpx_path).resolve()
    if not src.is_file() or src.suffix.lower() != ".hwpx":
        raise RunFixError("hwpx 파일이 아닙니다: %s" % src)
    now = now or datetime.datetime.now()
    out_dir = Path(out_root or ROOT / "output").resolve() / ("%s_%s_수정" % (src.stem, now.strftime("%Y%m%d-%H%M%S")))
    out_dir.mkdir(parents=True, exist_ok=False)

    sha_before = sha256_of(src)
    work = out_dir / "source.hwpx"
    shutil.copy2(src, work)
    if sha256_of(work) != sha_before:
        raise RunFixError("원본 복사본의 해시가 원본과 다릅니다.")
    code = git_info()
    meta = {"run_at": now.isoformat(timespec="seconds"),
            "code_version": "%s%s" % ((code["commit"] or "?")[:7],
                                       " (커밋 안 된 코드 변경 포함)" if code["uncommitted_code_changes"] else "")}
    errors, res = [], None
    try:
        # 모든 처리는 복사본(source.hwpx)으로 한다. 수정본 이름만 원본 파일명을 따른다
        res = format_hwpx(str(work), out_dir, doc_type, config_path, meta=meta, source_name=src.name)
        errors.extend(res["errors"])
    except FormatterError as e:
        errors.append(str(e))

    sha_after = sha256_of(src)
    if sha_after != sha_before:
        errors.append("원본 파일 해시가 실행 중 바뀌었습니다.")

    info = {
        "input": str(src),
        "source_sha256_before": sha_before,
        "source_sha256_after": sha_after,
        "source_unchanged": sha_before == sha_after,
        "run_at": meta["run_at"],
        "command": "python3 scripts/run_fix.py %s%s" % (src.name, " --doc-type " + doc_type if doc_type else ""),
        "doc_type": doc_type,
        "code": code,
        "ok": not errors,
        "errors": errors,
        "output": res["output"] if res and not errors else None,
        "check_before": res["summary_before"] if res else None,
        "check_after": res["summary_after"] if res else None,
        "applied_actions": len(res["fix"]["applied"]) if res else 0,
        "not_applied_actions": len(res["fix"]["not_applied"]) if res else 0,
        "files": sorted(p.name for p in out_dir.iterdir()) + ["run_info.json"],
    }
    with open(out_dir / "run_info.json", "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    return out_dir, info


def main(argv=None):
    ap = argparse.ArgumentParser(description="hwpx 를 검사 → 서식 수정 → 재검사·검증하고 결과를 한 폴더에 저장")
    ap.add_argument("input", help="수정할 .hwpx 파일")
    ap.add_argument("--doc-type", choices=["A", "B"], help="문서 유형(지정 시 TYPE A/B 특수 간격 적용)")
    ap.add_argument("--out-root", help="결과 상위 폴더 (기본: output/)")
    ap.add_argument("--config", help="표준값 설정 파일 (기본: config/format_standard.json)")
    args = ap.parse_args(argv)
    try:
        out_dir, info = run_fix(args.input, args.doc_type, args.out_root, args.config)
    except RunFixError as e:
        print("실패: %s" % e)
        return 2
    print("결과 폴더: %s" % out_dir)
    for name in info["files"]:
        print("  - %s" % name)
    print("원본 변경 없음: %s" % ("예" if info["source_unchanged"] else "아니오"))
    print("코드 버전: %s%s" % ((info["code"]["commit"] or "?")[:7],
                              " (커밋 안 된 코드 변경 포함)" if info["code"]["uncommitted_code_changes"] else ""))
    for label, key in (("수정 전", "check_before"), ("수정 후", "check_after")):
        s = info[key]
        if s:
            print("%s 검사: 총 %d건 — PASS %d, FAIL %d, UNKNOWN %d, SKIP %d"
                  % (label, s["total"], s["PASS"], s["FAIL"], s["UNKNOWN"], s["SKIP"]))
    print("적용한 수정 동작: %d개, 적용 못 한 동작: %d개" % (info["applied_actions"], info["not_applied_actions"]))
    if not info["ok"]:
        for e in info["errors"]:
            print("오류: %s" % e)
        return 2
    print("수정본: %s" % info["output"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

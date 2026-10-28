"""봇 요청 해석과 실행. 문서 처리는 반드시 저장소 실행기(scripts/run_fix.py, run_test.py)로만 한다.

- 메시지 해석: 수정(기본) / 검사 / 도움말, 문서 유형 A·B
- 실행: 첨부 hwpx 를 data/inbox/ 에 저장 → 실행기를 별도 프로세스로 실행 → 결과 폴더의 파일로 답장 구성
- 답장 내용은 실행기가 만든 파일(run_info.json, change_report.md, check_report.txt)만 옮긴다.
"""

import datetime
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

MODE_FIX, MODE_TEST, MODE_HELP = "fix", "test", "help"

# 메시지 하나에 담는 최대 글자 수(Mattermost 기본 MaxPostSize 16383 보다 여유 있게)
MAX_MESSAGE_CHARS = 15000

HELP_TEXT = """\
**letshwp 봇 사용법**
hwpx 파일을 첨부해서 저에게 DM 을 보내거나, 채널에서 @{bot} 를 멘션해 주세요.

| 메시지 | 동작 |
|---|---|
| (내용 없음) 또는 `수정` | 서식 검사 → 자동 수정 → 재검사·검증. 수정본 hwpx 와 수정 보고서를 보냅니다 |
| `검사` 또는 `테스트` | 서식 검사만 합니다(문서는 바꾸지 않음). 검사 보고서를 보냅니다 |
| `A` / `B` 함께 쓰기 | 문서 유형(TYPE A/B) 특수 간격을 적용합니다. 예: `수정 A`, `검사 B` |
| `도움말` | 이 안내 |

- 원본 파일은 바꾸지 않고 복사본으로 처리합니다.
- 검증에 실패하면 수정본을 보내지 않고 실패 사유만 알려 드립니다.
"""


def parse_request(message, bot_username=None):
    """메시지 → {"mode": fix|test|help, "doc_type": None|"A"|"B"}."""
    text = message or ""
    if bot_username:
        text = re.sub(r"@%s\b" % re.escape(bot_username), " ", text, flags=re.I)
    low = text.lower()
    fix_words = ("수정", "고쳐", "맞춰", "fix")
    test_words = ("검사", "테스트", "확인만", "check", "test")
    help_words = ("도움", "사용법", "help")
    if any(w in low for w in fix_words):
        mode = MODE_FIX
    elif any(w in low for w in test_words):
        mode = MODE_TEST
    elif any(w in low for w in help_words) or low.strip() in ("?", "？"):
        mode = MODE_HELP
    else:
        mode = MODE_FIX

    doc_type = None
    m = (re.search(r"(?:--doc-type|type|타입|유형)\s*[=:]?\s*([ab])(?![a-z])", text, re.I)
         or re.search(r"(?<![A-Za-z0-9])([AB])\s*(?:형|유형|타입)", text)
         or re.search(r"(?<![\w])([AB])(?![\w])", text))
    if m:
        doc_type = m.group(1).upper()
    return {"mode": mode, "doc_type": doc_type}


def safe_filename(name):
    """첨부 파일명에서 경로·제어 문자를 없앤다."""
    name = Path(str(name).replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().lstrip(".")
    return name or "document.hwpx"


def truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n…(길어서 여기까지만 표시합니다. 전체 내용은 첨부 파일을 확인해 주세요.)"


def summary_line(label, s):
    return ("%s 검사: 총 %d건 — PASS %d, FAIL %d, UNKNOWN %d, SKIP %d"
            % (label, s["total"], s["PASS"], s["FAIL"], s["UNKNOWN"], s["SKIP"]))


def code_version(info):
    code = (info or {}).get("code") or {}
    return "%s%s" % ((code.get("commit") or "?")[:7],
                     " (커밋 안 된 코드 변경 포함)" if code.get("uncommitted_code_changes") else "")


def run_runner(mode, hwpx_path, doc_type, out_root, timeout=600):
    """실행기를 별도 프로세스로 돌린다 → (종료 코드, 표준 출력, 결과 폴더 또는 None)."""
    script = ROOT / "scripts" / ("run_fix.py" if mode == MODE_FIX else "run_test.py")
    cmd = [sys.executable, str(script), str(hwpx_path), "--out-root", str(out_root)]
    if doc_type:
        cmd += ["--doc-type", doc_type]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    out_dir = None
    for line in proc.stdout.splitlines():
        if line.startswith("결과 폴더: "):
            out_dir = Path(line[len("결과 폴더: "):].strip())
    return proc.returncode, proc.stdout + proc.stderr, out_dir


def build_reply(mode, filename, returncode, output, out_dir):
    """실행 결과 → (메시지, 첨부할 파일 경로 목록). 실행기가 만든 파일 내용만 사용한다."""
    info = None
    if out_dir and (out_dir / "run_info.json").is_file():
        info = json.loads((out_dir / "run_info.json").read_text(encoding="utf-8"))
    ok = returncode == 0 and info is not None and info.get("ok")
    title = "서식 수정" if mode == MODE_FIX else "서식 검사"
    lines = []
    files = []
    if not ok:
        lines.append("**%s 실패** — `%s`" % (title, filename))
        if mode == MODE_FIX:
            lines.append("수정본을 만들지 않았습니다.")
        errors = (info or {}).get("errors") or [l for l in output.splitlines() if l.startswith(("실패", "오류"))]
        lines += ["- %s" % e for e in errors] or ["- 실행기 종료 코드 %s" % returncode]
        if info:
            lines.append("코드 버전: %s" % code_version(info))
            files.append(out_dir / "run_info.json")
        return "\n".join(lines), files

    lines.append("**%s 완료** — `%s`" % (title, filename))
    lines.append("코드 버전: %s · 원본 변경 없음: %s" % (code_version(info), "예" if info["source_unchanged"] else "아니오"))
    if mode == MODE_FIX:
        for label, key in (("수정 전", "check_before"), ("수정 후", "check_after")):
            if info.get(key):
                lines.append(summary_line(label, info[key]))
        lines.append("적용한 수정 동작: %d개, 적용 못 한 동작: %d개"
                     % (info["applied_actions"], info["not_applied_actions"]))
        report = out_dir / "change_report.md"
        files += [Path(info["output"]), report]
    else:
        if info.get("check_summary"):
            lines.append(summary_line("", info["check_summary"]).lstrip())
        report = out_dir / "check_report.txt"
        files.append(report)
    head = "\n".join(lines) + "\n\n---\n"
    body = report.read_text(encoding="utf-8") if report.is_file() else ""
    if mode == MODE_TEST:
        body = "```\n" + body + "\n```"
    return head + truncate(body, MAX_MESSAGE_CHARS - len(head) - 100), files


def save_attachment(data_dir, post_id, filename, content, now=None):
    now = now or datetime.datetime.now()
    d = Path(data_dir) / "inbox" / ("%s_%s" % (now.strftime("%Y%m%d-%H%M%S"), (post_id or "x")[:8]))
    d.mkdir(parents=True, exist_ok=True)
    path = d / safe_filename(filename)
    path.write_bytes(content)
    return path


def cleanup_old(data_dir, keep_days, now=None):
    """data/inbox, data/output 에서 keep_days 보다 오래된 폴더를 지운다(사용자 문서 보관 기간). 지운 개수."""
    if not keep_days or keep_days <= 0:
        return 0
    limit = (now or time.time()) - keep_days * 86400
    removed = 0
    for sub in ("inbox", "output"):
        base = Path(data_dir) / sub
        if not base.is_dir():
            continue
        for p in base.iterdir():
            if p.is_dir() and p.stat().st_mtime < limit:
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
    return removed

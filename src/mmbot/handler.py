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
import unicodedata
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
| (내용 없음) 또는 `수정` | 서식 검사 → 자동 수정 → 재검사·검증. 수정본 hwpx 를 보내고, 바꾼 내용과 직접 확인할 항목을 알려 드립니다 |
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
    """첨부 파일명에서 경로·제어 문자를 없앤다. macOS 가 보내는 자모 분리형(NFD) 한글은 NFC 로 합친다."""
    name = unicodedata.normalize("NFC", Path(str(name).replace("\\", "/")).name)
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().lstrip(".")
    return name or "document.hwpx"


def truncate(text, limit, note="…(길어서 여기까지만 표시합니다.)"):
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n" + note


def md_sections(md):
    """마크다운을 '## ' 제목 기준으로 나눈다 → [(제목, [본문 줄])]. 첫 제목 앞 부분은 제목 ''."""
    out = [("", [])]
    for line in md.splitlines():
        if line.startswith("## "):
            out.append((line[3:].strip(), []))
        else:
            out[-1][1].append(line)
    return out


def _section(sections, prefix):
    for title, body in sections:
        if title.startswith(prefix):
            return [l for l in body]
    return []


def fix_message(filename, report_md, limit=MAX_MESSAGE_CHARS):
    """수정 보고서(change_report.md)에서 채팅용 메시지를 만든다.
    수정한 내용(2절)과 직접 확인이 필요한 항목(4절)만 옮기고, 검증 통과·적합 집계는 싣지 않는다."""
    secs = md_sections(report_md)
    changes = _section(secs, "2.")
    rules = [l for l in changes if l.startswith("### ")]
    count = sum(int(m.group(1)) for m in (re.search(r"— (\d+)건", l) for l in rules) if m)
    manual = _section(secs, "4.")
    has_manual = any(l.startswith("### ") for l in manual)
    caution = [l for l in _section(secs, "6.") if "줄 나눔" in l]

    head = ("**서식 수정 완료** — `%s` (수정 %d건)" % (filename, count) if count
            else "**서식 수정 완료** — `%s` (표준과 다른 서식이 없어 바꾼 것이 없습니다)" % filename)

    def compose(detail):
        parts = [head, ""]
        if count:
            parts += detail
        if has_manual:
            parts += ["", "#### 직접 확인 필요"] + [l.replace("### ", "##### ", 1) if l.startswith("### ") else l
                                                   for l in manual]
        if count:
            parts += [""] + caution
        return re.sub(r"\n{3,}", "\n\n", "\n".join(parts)).strip() + "\n"

    msg = compose([l.replace("### ", "#### ", 1) if l.startswith("### ") else l for l in changes])
    if len(msg) > limit:
        # 위치별 표가 너무 길면 항목별 건수만 싣는다
        msg = compose(["- %s" % l[4:] for l in rules] + ["", "_(수정 위치가 많아 항목별 건수만 표시합니다.)_"])
    return truncate(msg, limit)


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
    """실행 결과 → (메시지, 첨부할 파일 경로 목록). 실행기가 만든 파일 내용만 사용한다.
    수정: 수정본 hwpx 하나만 첨부하고, 메시지에는 수정한 내용과 직접 확인할 항목만 싣는다.
    검사: 검사 보고서(check_report.txt)를 첨부하고 내용을 함께 싣는다."""
    info = None
    if out_dir and (out_dir / "run_info.json").is_file():
        info = json.loads((out_dir / "run_info.json").read_text(encoding="utf-8"))
    ok = returncode == 0 and info is not None and info.get("ok")
    title = "서식 수정" if mode == MODE_FIX else "서식 검사"
    if not ok:
        lines = ["**%s 실패** — `%s`" % (title, filename)]
        if mode == MODE_FIX:
            lines.append("수정본을 만들지 않았습니다.")
        errors = (info or {}).get("errors") or [l for l in output.splitlines() if l.startswith(("실패", "오류"))]
        lines += ["- %s" % e for e in errors] or ["- 실행기 종료 코드 %s" % returncode]
        if info:
            lines.append("코드 버전: %s" % code_version(info))
        return "\n".join(lines), []

    if mode == MODE_FIX:
        report = out_dir / "change_report.md"
        md = report.read_text(encoding="utf-8") if report.is_file() else ""
        return fix_message(filename, md), [Path(info["output"])]

    lines = ["**%s 완료** — `%s`" % (title, filename),
             "코드 버전: %s · 원본 변경 없음: %s" % (code_version(info), "예" if info["source_unchanged"] else "아니오")]
    if info.get("check_summary"):
        lines.append(summary_line("", info["check_summary"]).lstrip())
    report = out_dir / "check_report.txt"
    head = "\n".join(lines) + "\n\n---\n"
    body = "```\n" + (report.read_text(encoding="utf-8") if report.is_file() else "") + "\n```"
    return head + truncate(body, MAX_MESSAGE_CHARS - len(head) - 100,
                           "…(길어서 여기까지만 표시합니다. 전체 내용은 첨부 파일을 확인해 주세요.)"), [report]


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

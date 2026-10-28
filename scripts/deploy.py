"""배포 실행기: 커밋된 코드를 Tailscale 로 연결된 서버에 올리고 Mattermost 봇을 재시작한다.

사용
    python3 scripts/deploy.py deploy [--allow-unpushed]   배포(기본 동작)
    python3 scripts/deploy.py setup                       서버 최초 준비(폴더, 설정 파일, systemd 서비스)
    python3 scripts/deploy.py status                      현재 배포 버전·서비스 상태
    python3 scripts/deploy.py rollback                    직전 배포 버전으로 되돌리기
    python3 scripts/deploy.py logs [-n 50]                봇 로그
    python3 scripts/deploy.py join-bot                    봇 계정을 팀·채널에 추가(계정은 시스템 콘솔에서 생성)
    python3 scripts/deploy.py set-token                   봇 토큰 교체: 숨김 입력(또는 표준 입력) → 서버에서 확인 후 저장·재시작
    python3 scripts/deploy.py set-env KEY=VALUE           서버 설정 파일 값 변경(토큰 제외)
    python3 scripts/deploy.py upload-fonts <ttf...>       글꼴 파일을 서버 <base>/shared/fonts/ 에 올린다(저장소에 넣지 않음)

배포 순서(하나라도 실패하면 중단, 서버 반영 후 실패하면 직전 버전으로 자동 복구)
    1. 로컬 검사: 커밋 안 된 변경이 없고, HEAD 가 origin/main 에 push 되어 있어야 한다
    2. 로컬 전체 테스트
    3. git archive HEAD + DEPLOY_VERSION.json → 서버 <base>/releases/<시각>_<커밋>/ 에 풀기
    4. 서버에서 전체 테스트(실패 시 해당 릴리스 삭제)
    5. <base>/current 링크 교체 → 서비스 재시작 → 봇이 Mattermost 에 연결됐는지 확인(status.json)
    6. 실패 시 이전 릴리스로 링크 복구·재시작(종료 코드 4)

서버 폴더: <base>/releases/, <base>/current → 현재 릴리스, <base>/shared/bot.env, <base>/shared/data/
설정: deploy/deploy.json. 종료 코드: 0 성공, 2 로컬 검사·테스트 실패, 3 서버 테스트 실패, 4 서버 반영 실패(복구함), 5 연결 실패
"""

import argparse
import datetime
import getpass
import io
import re
import json
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "deploy" / "deploy.json"


class DeployError(RuntimeError):
    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


def load_config(path=CONFIG):
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    for k in ("host", "user", "base_dir", "service", "service_scope", "python"):
        if not cfg.get(k):
            raise DeployError("deploy.json 에 %s 가 없습니다." % k)
    if cfg["service_scope"] not in ("system", "user"):
        raise DeployError("service_scope 는 system 또는 user 여야 합니다.")
    cfg.setdefault("keep_releases", 5)
    cfg.setdefault("run_user", cfg["user"])  # 봇을 실행할 계정(SSH 계정과 다를 수 있다)
    return cfg


def systemctl(cfg):
    return "sudo -n systemctl" if cfg["service_scope"] == "system" else "systemctl --user"


def journalctl(cfg):
    return "sudo -n journalctl" if cfg["service_scope"] == "system" else "journalctl --user"


def git(*args):
    p = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def check_git(allow_unpushed=False):
    """배포 가능한 커밋인지 확인한다 → {"commit", "branch"}."""
    _, status, _ = git("status", "--porcelain")
    if status:
        raise DeployError("커밋 안 된 변경이 있어 배포하지 않습니다(배포는 커밋된 코드만):\n" + status)
    _, commit, _ = git("rev-parse", "HEAD")
    _, branch, _ = git("rev-parse", "--abbrev-ref", "HEAD")
    if not allow_unpushed:
        rc, _, err = git("fetch", "origin", "main", "--quiet")
        if rc != 0:
            raise DeployError("origin 을 확인할 수 없습니다(--allow-unpushed 로 건너뛸 수 있음): " + err)
        rc, _, _ = git("merge-base", "--is-ancestor", "HEAD", "origin/main")
        if rc != 0:
            raise DeployError("HEAD(%s)가 origin/main 에 없습니다. 먼저 push 하세요." % commit[:7])
    return {"commit": commit, "branch": branch}


def run_local_tests():
    p = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=ROOT,
                       capture_output=True, text=True)
    tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-3:])
    if p.returncode != 0:
        raise DeployError("로컬 테스트 실패 — 배포 중단\n" + (p.stdout + p.stderr)[-3000:])
    return tail


def release_name(commit, now):
    return "%s_%s" % (now.strftime("%Y%m%d-%H%M%S"), commit[:7])


def build_package(commit, version):
    """git archive 결과에 DEPLOY_VERSION.json 을 더한 tar 바이트."""
    p = subprocess.run(["git", "archive", "--format=tar", commit], cwd=ROOT, capture_output=True)
    if p.returncode != 0:
        raise DeployError("git archive 실패: " + p.stderr.decode("utf-8", "replace"))
    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(p.stdout)) as src, tarfile.open(fileobj=out, mode="w") as dst:
        for m in src.getmembers():
            dst.addfile(m, src.extractfile(m) if m.isfile() else None)
        data = json.dumps(version, ensure_ascii=False, indent=2).encode("utf-8")
        info = tarfile.TarInfo("DEPLOY_VERSION.json")
        info.size = len(data)
        info.mtime = int(datetime.datetime.now().timestamp())
        info.mode = 0o644
        dst.addfile(info, io.BytesIO(data))
    return out.getvalue()


def ssh(cfg, script, stdin=None, timeout=600):
    """서버에서 bash 스크립트를 실행한다 → (종료 코드, 출력)."""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "%s@%s" % (cfg["user"], cfg["host"]),
           "bash -c %s" % shlex.quote(script)]
    p = subprocess.run(cmd, input=stdin, capture_output=True, timeout=timeout)
    out = (p.stdout + p.stderr).decode("utf-8", "replace")
    if p.returncode == 255:
        raise DeployError("서버 접속 실패(%s@%s): %s" % (cfg["user"], cfg["host"], out.strip()), 5)
    return p.returncode, out


def q(s):
    return shlex.quote(str(s))


def health_script(cfg):
    """서비스 재시작 후 봇이 Mattermost 에 연결될 때까지(최대 30초) 기다린다. 성공 시 HEALTH_OK 출력."""
    return """
rm -f {status}
{sc} restart {svc}
ok=0
for i in $(seq 1 30); do
  sleep 1
  if grep -q '"connected": true' {status} 2>/dev/null; then ok=1; break; fi
done
if [ "$ok" = 1 ] && {sc} is-active --quiet {svc}; then echo HEALTH_OK; else
  echo "봇이 30초 안에 Mattermost 에 연결되지 않았습니다."
  {jc} -u {svc} -n 30 --no-pager 2>/dev/null || true
fi
""".format(status=q(cfg["base_dir"] + "/shared/data/status.json"), sc=systemctl(cfg), svc=q(cfg["service"]),
           jc=journalctl(cfg))


def activate_script(cfg, name):
    base, keep = cfg["base_dir"], int(cfg["keep_releases"])
    return """
set -u
BASE={base}; REL="$BASE/releases/{name}"; PY={py}
cd "$REL" || exit 10
if ! "$PY" -m unittest discover -s tests > "$REL/server_tests.log" 2>&1; then
  echo SERVER_TESTS_FAILED; tail -n 40 "$REL/server_tests.log"; rm -rf "$REL"; exit 3
fi
echo "서버 테스트: $(grep -E '^(Ran|OK|FAILED)' "$REL/server_tests.log" | tr '\\n' ' ')"
PREV=$(readlink -f "$BASE/current" 2>/dev/null || true)
ln -sfn "$REL" "$BASE/current.new" && mv -Tf "$BASE/current.new" "$BASE/current"
HEALTH=$( {health} )
echo "$HEALTH"
if echo "$HEALTH" | grep -q HEALTH_OK; then
  echo "$(date '+%Y-%m-%dT%H:%M:%S') deploy {name}" >> "$BASE/deploy_history.log"
  ls -1d "$BASE"/releases/*/ | sort | head -n -{keep} | while read d; do
    [ "$(readlink -f "$d")" = "$(readlink -f "$BASE/current")" ] || rm -rf "$d"
  done
  exit 0
fi
if [ -n "$PREV" ] && [ -d "$PREV" ] && [ "$PREV" != "$REL" ]; then
  ln -sfn "$PREV" "$BASE/current.new" && mv -Tf "$BASE/current.new" "$BASE/current"
  {sc} restart {svc}
  echo "ROLLED_BACK $(basename "$PREV")"
  echo "$(date '+%Y-%m-%dT%H:%M:%S') failed {name} -> rollback $(basename "$PREV")" >> "$BASE/deploy_history.log"
fi
exit 4
""".format(base=q(base), name=name, py=q(cfg["python"]), health=health_script(cfg), keep=keep,
           sc=systemctl(cfg), svc=q(cfg["service"]))


def cmd_deploy(cfg, allow_unpushed=False, now=None):
    now = now or datetime.datetime.now()
    g = check_git(allow_unpushed)
    print("1) 배포 대상: %s (%s)" % (g["commit"][:7], g["branch"]))
    print("2) 로컬 테스트: %s" % run_local_tests().replace("\n", " "))
    name = release_name(g["commit"], now)
    version = {"commit": g["commit"], "branch": g["branch"], "release": name,
               "deployed_at": now.isoformat(timespec="seconds")}
    tar = build_package(g["commit"], version)
    rel = "%s/releases/%s" % (cfg["base_dir"], name)
    rc, out = ssh(cfg, "mkdir -p %s && tar -xf - -C %s" % (q(rel), q(rel)), stdin=tar)
    if rc != 0:
        raise DeployError("서버에 코드를 풀지 못했습니다: " + out, 4)
    print("3) 업로드: %s (%d KB)" % (rel, len(tar) // 1024))
    rc, out = ssh(cfg, activate_script(cfg, name))
    print(out.strip())
    if rc == 3:
        raise DeployError("서버 테스트 실패 — 배포 중단(현재 버전 유지)", 3)
    if rc != 0:
        raise DeployError("서버 반영 실패 — %s" % ("직전 버전으로 복구했습니다" if "ROLLED_BACK" in out
                                               else "복구할 이전 버전이 없습니다"), 4)
    print("4) 배포 완료: %s — 봇 연결 확인됨" % name)
    return name


def cmd_setup(cfg):
    unit = (ROOT / "deploy" / "letshwp-bot.service").read_text(encoding="utf-8").format(**cfg)
    env = (ROOT / "deploy" / "bot.env.example").read_text(encoding="utf-8").replace(
        "/opt/letshwp", cfg["base_dir"])
    base = q(cfg["base_dir"])
    if cfg["service_scope"] == "system":
        ru = q(cfg["run_user"])
        mk = ("id -u {ru} >/dev/null 2>&1 || sudo -n useradd --system --home-dir {b} --shell /usr/sbin/nologin {ru}\n"
              "sudo -n mkdir -p {b}").format(b=base, ru=ru)
        unit_path = "/etc/systemd/system/%s.service" % cfg["service"]
        install = "printf %%s %s | sudo -n tee %s > /dev/null" % (q(unit), q(unit_path))
    else:
        mk = "mkdir -p %s" % base
        unit = unit.replace("User=%s\n" % cfg["run_user"], "").replace("multi-user.target", "default.target")
        install = "mkdir -p ~/.config/systemd/user && printf %%s %s > ~/.config/systemd/user/%s.service" % (
            q(unit), cfg["service"])
    script = """
set -e
{mk}
mkdir -p {b}/releases {b}/shared/data
if [ ! -f {b}/shared/bot.env ]; then printf %s {env} > {b}/shared/bot.env; echo "설정 파일 생성: {b}/shared/bot.env"; fi
chmod 600 {b}/shared/bot.env
{own}
{install}
{sc} daemon-reload
{sc} enable {svc}
echo SETUP_OK
""".format(mk=mk, b=base, env=q(env), install=install, sc=systemctl(cfg), svc=q(cfg["service"]), own=own(cfg))
    rc, out = ssh(cfg, script)
    print(out.strip())
    if rc != 0 or "SETUP_OK" not in out:
        raise DeployError("서버 준비 실패", 4)


def own(cfg):
    """봇 실행 계정이 SSH 계정과 다르면 설정·데이터 폴더 소유자를 실행 계정으로 맞춘다(코드는 읽기만 한다)."""
    if cfg["run_user"] == cfg["user"]:
        return ":"
    b = q(cfg["base_dir"])
    return "sudo -n chown -R {ru}: {b}/shared".format(ru=q(cfg["run_user"]), b=b)


def set_env_script(cfg, key):
    """설정 파일의 key 값을 표준 입력으로 받은 값으로 바꾼다(값이 명령줄·로그에 남지 않게)."""
    path = cfg["base_dir"] + "/shared/bot.env"
    py = ("import sys,re;p=sys.argv[1];k=sys.argv[2];v=sys.stdin.read().strip();"
          "s=open(p,encoding='utf-8').read();"
          "s=re.sub(r'(?m)^'+re.escape(k)+r'=.*$',lambda m:k+'='+v,s) if re.search(r'(?m)^'+re.escape(k)+'=',s)"
          " else s.rstrip(chr(10))+chr(10)+k+'='+v+chr(10);"
          "open(p,'w',encoding='utf-8').write(s);print('저장:',k)")
    return "%s -c %s %s %s && chmod 600 %s && %s" % (q(cfg["python"]), q(py), q(path), q(key), q(path), own(cfg))


def cmd_set_env(cfg, key, value):
    if not key or "=" in key or "\n" in value:
        raise DeployError("잘못된 설정 키/값")
    rc, out = ssh(cfg, set_env_script(cfg, key), stdin=value.encode("utf-8"))
    print(out.strip())
    if rc != 0:
        raise DeployError("설정 저장 실패", 4)


TOKEN_RE = re.compile(r"^[a-z0-9]{26}$")


def read_token(stdin=None):
    """토큰을 숨김 입력(터미널) 또는 표준 입력으로 받는다. 화면·명령줄·대화에 남지 않게 한다."""
    stdin = stdin or sys.stdin
    if stdin.isatty():
        token = getpass.getpass("새 봇 토큰(Access Token)을 붙여 넣고 Enter — 입력 내용은 보이지 않습니다: ")
    else:
        token = stdin.read()
    token = token.strip()
    if not TOKEN_RE.match(token):
        raise DeployError("토큰 형식이 아닙니다(영문 소문자·숫자 26자). Token ID 가 아닌 Access Token 인지 확인하세요.")
    return token


def set_token_script(cfg):
    """표준 입력의 토큰으로 서버에서 Mattermost 접속을 먼저 확인하고(봇 계정이어야 함),
    통과하면 설정 파일에 저장한 뒤 봇을 재시작해 연결을 확인한다. 확인 실패 시 기존 토큰을 유지한다."""
    env = cfg["base_dir"] + "/shared/bot.env"
    verify = ("import json,re,sys,urllib.request;t=sys.stdin.read().strip();"
              "u=re.search(r'(?m)^MM_URL=(.*)$',open(sys.argv[1],encoding='utf-8').read()).group(1).strip();"
              "r=urllib.request.Request(u.rstrip('/')+'/api/v4/users/me',headers={'Authorization':'Bearer '+t});"
              "d=json.load(urllib.request.urlopen(r,timeout=15));"
              "sys.exit('봇 계정 토큰이 아닙니다') if not d.get('is_bot') else print('토큰 확인: @'+d['username'])")
    return """
set -u
TOKEN=$(cat)
if ! printf %s "$TOKEN" | {py} -c {verify} {env} 2>/tmp/letshwp_token_err; then
  echo "토큰 확인 실패 — 기존 토큰을 그대로 둡니다."; tail -n 1 /tmp/letshwp_token_err; rm -f /tmp/letshwp_token_err; exit 3
fi
rm -f /tmp/letshwp_token_err
printf %s "$TOKEN" | {{ {save}; }} || exit 4
unset TOKEN
HEALTH=$( {health} )
echo "$HEALTH"
echo "$HEALTH" | grep -q HEALTH_OK || exit 4
""".format(py=q(cfg["python"]), verify=q(verify), env=q(env), save=set_env_script(cfg, "MM_TOKEN"),
           health=health_script(cfg))


def cmd_set_token(cfg, token):
    rc, out = ssh(cfg, set_token_script(cfg), stdin=token.encode("utf-8"))
    print(out.strip())
    if rc == 3:
        raise DeployError("토큰이 Mattermost 에서 거부되어 저장하지 않았습니다(기존 토큰 유지)", 3)
    if rc != 0:
        raise DeployError("토큰 저장 후 봇 연결 확인 실패", 4)
    print("토큰 교체 완료 — 이전 토큰은 Mattermost 에서 삭제해도 됩니다.")


def join_bot_script(cfg):
    """Mattermost 컨테이너의 mmctl(로컬 모드)로 봇 계정을 팀·채널에 넣는다.
    봇 계정 생성은 mmctl 로컬 모드에서 지원되지 않아 시스템 콘솔(통합 > 봇 계정)에서 만든다."""
    mm = cfg.get("mattermost") or {}
    for k in ("container", "bot_username", "team"):
        if not mm.get(k):
            raise DeployError("deploy.json 의 mattermost.%s 가 없습니다." % k)
    mmctl = "docker exec %s mmctl --local" % q(mm["container"])
    bot, team = q(mm["bot_username"]), q(mm["team"])
    lines = ["set -e",
             "{m} user search {b} >/dev/null 2>&1 || {{ echo '봇 계정이 없습니다: {b}'; exit 1; }}".format(m=mmctl, b=bot),
             "{m} team users add {t} {b}; echo '팀 추가: {t}'".format(m=mmctl, t=team, b=bot)]
    for ch in mm.get("channels") or []:
        c = q("%s:%s" % (mm["team"], ch))
        lines.append("{m} channel users add {c} {b}; echo '채널 추가: {c}'".format(m=mmctl, c=c, b=bot))
    lines.append("echo JOIN_BOT_OK")
    return "\n".join(lines)


def cmd_join_bot(cfg):
    rc, out = ssh(cfg, join_bot_script(cfg))
    print(out.strip())
    if rc != 0 or "JOIN_BOT_OK" not in out:
        raise DeployError("봇 팀·채널 추가 실패", 4)


def cmd_upload_fonts(cfg, files):
    """글꼴 파일(.ttf/.ttc)을 서버 글꼴 폴더에 올린다. 봇은 LETSHWP_FONT_DIR 로 이 폴더를 읽는다."""
    paths = [Path(f) for f in files]
    bad = [str(p) for p in paths if not p.is_file() or p.suffix.lower() not in (".ttf", ".ttc", ".otf")]
    if bad or not paths:
        raise DeployError("글꼴 파일(.ttf/.ttc)이 아님: %s" % (", ".join(bad) or "(없음)"))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for p in paths:
            t.add(str(p), arcname=p.name)
    fonts = cfg["base_dir"] + "/shared/fonts"
    script = "mkdir -p {f} && tar -xf - -C {f} && {own} && ls -1 {f}".format(f=q(fonts), own=own(cfg))
    rc, out = ssh(cfg, script, stdin=buf.getvalue())
    print(out.strip())
    if rc != 0:
        raise DeployError("글꼴 업로드 실패", 4)
    cmd_set_env(cfg, "LETSHWP_FONT_DIR", fonts)
    print("글꼴 %d개 업로드 완료. 다음 배포(또는 재시작) 때부터 봇이 사용합니다." % len(paths))


def cmd_status(cfg):
    b = q(cfg["base_dir"])
    script = """
echo "현재 릴리스: $(basename "$(readlink -f {b}/current 2>/dev/null)" 2>/dev/null)"
cat {b}/current/DEPLOY_VERSION.json 2>/dev/null
echo "서비스: $({sc} is-active {svc} 2>&1)"
echo "봇 상태:"; cat {b}/shared/data/status.json 2>/dev/null || echo "(없음)"
echo "릴리스 목록:"; ls -1 {b}/releases 2>/dev/null
echo "최근 배포 이력:"; tail -n 5 {b}/deploy_history.log 2>/dev/null
""".format(b=b, sc=systemctl(cfg), svc=q(cfg["service"]))
    print(ssh(cfg, script)[1].strip())


def cmd_rollback(cfg):
    b = q(cfg["base_dir"])
    script = """
set -u
CUR=$(basename "$(readlink -f {b}/current)")
PREV=$(ls -1 {b}/releases | sort | awk -v c="$CUR" '$0==c{{print p; exit}} {{p=$0}}')
if [ -z "$PREV" ]; then echo "되돌릴 이전 릴리스가 없습니다(현재 $CUR)"; exit 4; fi
ln -sfn {b}/releases/"$PREV" {b}/current.new && mv -Tf {b}/current.new {b}/current
echo "되돌림: $CUR -> $PREV"
HEALTH=$( {health} )
echo "$HEALTH"
echo "$(date '+%Y-%m-%dT%H:%M:%S') rollback $CUR -> $PREV" >> {b}/deploy_history.log
echo "$HEALTH" | grep -q HEALTH_OK || exit 4
""".format(b=b, health=health_script(cfg))
    rc, out = ssh(cfg, script)
    print(out.strip())
    if rc != 0:
        raise DeployError("되돌리기 실패", 4)


def cmd_logs(cfg, n):
    print(ssh(cfg, "%s -u %s -n %d --no-pager" % (journalctl(cfg), q(cfg["service"]), n))[1].strip())


def main(argv=None):
    ap = argparse.ArgumentParser(description="letshwp 서버 배포")
    ap.add_argument("command", nargs="?", default="deploy",
                    choices=["deploy", "setup", "join-bot", "status", "rollback", "logs", "set-token", "set-env", "upload-fonts"])
    ap.add_argument("arg", nargs="*", help="set-env 의 KEY=VALUE 또는 upload-fonts 의 글꼴 파일들")
    ap.add_argument("--allow-unpushed", action="store_true", help="origin/main 에 없는 커밋도 배포")
    ap.add_argument("-n", type=int, default=50, help="logs 줄 수")
    ap.add_argument("--config", default=str(CONFIG))
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.command == "deploy":
            cmd_deploy(cfg, args.allow_unpushed)
        elif args.command == "setup":
            cmd_setup(cfg)
        elif args.command == "join-bot":
            cmd_join_bot(cfg)
        elif args.command == "status":
            cmd_status(cfg)
        elif args.command == "rollback":
            cmd_rollback(cfg)
        elif args.command == "logs":
            cmd_logs(cfg, args.n)
        elif args.command == "set-token":
            cmd_set_token(cfg, read_token())
        elif args.command == "upload-fonts":
            cmd_upload_fonts(cfg, args.arg)
        elif args.command == "set-env":
            key, _, value = (args.arg[0] if args.arg else "").partition("=")
            if key == "MM_TOKEN":
                raise DeployError("토큰은 set-token 으로(표준 입력) 저장하세요.")
            cmd_set_env(cfg, key, value)
    except DeployError as e:
        print("실패: %s" % e)
        return e.code
    except subprocess.TimeoutExpired:
        print("실패: 서버 응답 시간 초과")
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())

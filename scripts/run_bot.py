"""Mattermost 봇 실행기.

사용
    python3 scripts/run_bot.py --env <설정 파일>            봇 실행(서버에서는 systemd 가 실행)
    python3 scripts/run_bot.py --env <설정 파일> --check    토큰·서버 연결만 확인하고 종료(배포 후 점검용)

설정 파일(KEY=VALUE, 예: deploy/bot.env.example)
    MM_URL              Mattermost 주소 (예: http://127.0.0.1:8065)
    MM_TOKEN            봇 계정 액세스 토큰 (저장소에 넣지 않는다)
    LETSHWP_DATA_DIR    첨부 파일·결과 저장 폴더
    LETSHWP_MAX_FILE_MB 처리할 최대 파일 크기(기본 50)
    LETSHWP_KEEP_DAYS   첨부·결과 보관 일수(기본 30, 0 이면 지우지 않음)
    MM_VERIFY_TLS       https 인증서 검증(기본 true)

종료 코드: 0 정상, 2 설정 오류 또는 연결 실패
"""

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mmbot.bot import Bot  # noqa: E402
from mmbot.client import MattermostClient, MattermostError  # noqa: E402


def load_env(path):
    """KEY=VALUE 파일을 읽는다. 빈 줄·# 주석은 건너뛰고 값의 따옴표는 벗긴다."""
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def settings(env):
    missing = [k for k in ("MM_URL", "MM_TOKEN", "LETSHWP_DATA_DIR") if not env.get(k)]
    if missing:
        raise ValueError("설정 누락: %s" % ", ".join(missing))
    return {
        "url": env["MM_URL"],
        "token": env["MM_TOKEN"],
        "data_dir": env["LETSHWP_DATA_DIR"],
        "max_file_mb": float(env.get("LETSHWP_MAX_FILE_MB") or 50),
        "keep_days": int(env.get("LETSHWP_KEEP_DAYS") or 30),
        "verify_tls": (env.get("MM_VERIFY_TLS") or "true").lower() not in ("0", "false", "no"),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="letshwp Mattermost 봇")
    ap.add_argument("--env", help="설정 파일 (없으면 환경 변수 사용)")
    ap.add_argument("--check", action="store_true", help="연결·토큰만 확인하고 종료")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        env = dict(os.environ)
        if args.env:
            env.update(load_env(args.env))
        cfg = settings(env)
    except (OSError, ValueError) as e:
        print("설정 오류: %s" % e)
        return 2
    client = MattermostClient(cfg["url"], cfg["token"], verify_tls=cfg["verify_tls"])
    if args.check:
        try:
            me = client.get_me()
        except MattermostError as e:
            print("연결 실패: %s" % e)
            return 2
        print("연결 정상: @%s (봇 계정: %s)" % (me["username"], "예" if me.get("is_bot") else "아니오"))
        return 0
    try:
        Bot(client, cfg["data_dir"], cfg["max_file_mb"], cfg["keep_days"]).run()
    except MattermostError as e:
        print("연결 실패: %s" % e)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""봇 본체: WebSocket 으로 새 글(posted 이벤트)을 받아 작업 큐에 넣고, 작업은 한 번에 하나씩 처리한다."""

import json
import logging
import queue
import socket
import threading
import time
from pathlib import Path

from . import handler
from .client import MattermostError
from .websocket import WebSocketError

log = logging.getLogger("letshwp.bot")


def should_handle(event, bot_id, bot_username):
    """posted 이벤트 → 처리할 글(dict) 또는 None. DM 이거나 봇을 멘션한 사람의 글만 처리한다."""
    if event.get("event") != "posted":
        return None
    data = event.get("data") or {}
    try:
        post = json.loads(data.get("post") or "{}")
    except ValueError:
        return None
    if not post or post.get("user_id") == bot_id or post.get("type"):
        return None
    if (post.get("props") or {}).get("from_bot") in ("true", True):
        return None
    if data.get("channel_type") == "D":
        return post
    try:
        mentions = json.loads(data.get("mentions") or "[]")
    except ValueError:
        mentions = []
    if bot_id in mentions or ("@%s" % bot_username).lower() in (post.get("message") or "").lower():
        return post
    return None


class Bot:
    def __init__(self, client, data_dir, max_file_mb=50, keep_days=30, runner_timeout=600,
                 ws_timeout=30, idle_limit=90):
        self.client = client
        self.data_dir = Path(data_dir)
        self.max_bytes = int(max_file_mb * 1024 * 1024)
        self.keep_days = keep_days
        self.runner_timeout = runner_timeout
        self.ws_timeout = ws_timeout      # 이 시간 동안 프레임이 없으면 ping 을 보낸다
        self.idle_limit = idle_limit      # 이 시간 동안 어떤 프레임(pong 포함)도 없으면 끊고 다시 접속한다
        self.jobs = queue.Queue()
        self.me = None
        self.status_path = self.data_dir / "status.json"

    # --- 상태 기록(배포 후 정상 동작 확인용) ---
    def write_status(self, **kw):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        st = {}
        if self.status_path.is_file():
            try:
                st = json.loads(self.status_path.read_text(encoding="utf-8"))
            except ValueError:
                st = {}
        st.update(kw)
        st["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.status_path.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")

    def reply(self, post, message, files=()):
        root = post.get("root_id") or post["id"]
        file_ids = [self.client.upload_file(post["channel_id"], str(p)) for p in files if Path(p).is_file()]
        self.client.create_post(post["channel_id"], message, root_id=root, file_ids=file_ids[:10])

    # --- 글 접수 ---
    def on_post(self, post):
        req = handler.parse_request(post.get("message"), self.me["username"])
        hwpx = []
        for fid in post.get("file_ids") or []:
            info = self.client.get_file_info(fid)
            if (info.get("extension") or "").lower() == "hwpx":
                hwpx.append(info)
        if req["mode"] == handler.MODE_HELP or not hwpx:
            msg = handler.HELP_TEXT.format(bot=self.me["username"])
            if req["mode"] != handler.MODE_HELP:
                msg = "hwpx 파일이 첨부되지 않았습니다. (hwp 는 지원하지 않습니다. 한글에서 hwpx 로 저장해 주세요.)\n\n" + msg
            self.reply(post, msg)
            return
        for info in hwpx:
            if info.get("size", 0) > self.max_bytes:
                self.reply(post, "`%s` 은(는) %dMB 를 넘어서 처리하지 않습니다."
                           % (info.get("name"), self.max_bytes // (1024 * 1024)))
                continue
            self.jobs.put((post, info, req))
            self.reply(post, "`%s` 접수했습니다 — %s%s. 잠시만 기다려 주세요. (대기 %d건)"
                       % (info.get("name"), "서식 수정" if req["mode"] == handler.MODE_FIX else "서식 검사",
                          " (TYPE %s)" % req["doc_type"] if req["doc_type"] else "", self.jobs.qsize()))

    # --- 작업 처리 ---
    def process(self, post, info, req):
        content = self.client.download_file(info["id"])
        path = handler.save_attachment(self.data_dir, post.get("id"), info.get("name"), content)
        log.info("처리 시작 mode=%s doc_type=%s file=%s", req["mode"], req["doc_type"], path)
        rc, output, out_dir = handler.run_runner(req["mode"], path, req["doc_type"],
                                                 self.data_dir / "output", self.runner_timeout)
        log.info("처리 끝 rc=%s out=%s", rc, out_dir)
        message, files = handler.build_reply(req["mode"], info.get("name"), rc, output, out_dir)
        self.reply(post, message, files)

    def worker(self):
        while True:
            post, info, req = self.jobs.get()
            try:
                self.process(post, info, req)
            except Exception as e:  # 한 작업의 실패가 봇 전체를 멈추지 않게 한다
                log.exception("작업 실패")
                try:
                    self.reply(post, "**처리 중 오류** — `%s`\n- %s" % (info.get("name"), e))
                except Exception:
                    log.exception("오류 답장 실패")
            finally:
                self.jobs.task_done()

    def cleanup_loop(self):
        while True:
            try:
                n = handler.cleanup_old(self.data_dir, self.keep_days)
                if n:
                    log.info("보관 기간(%d일) 지난 폴더 %d개 삭제", self.keep_days, n)
            except Exception:
                log.exception("정리 실패")
            time.sleep(6 * 3600)

    # --- 이벤트 수신 ---
    def listen_once(self):
        ws = self.client.websocket(timeout=self.ws_timeout)
        log.info("WebSocket 연결됨")
        self.write_status(connected=True, connected_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        try:
            while True:
                try:
                    raw = ws.recv_text()
                except socket.timeout:
                    # 이벤트가 없어도 ping 에 대한 pong 이 오면 연결은 살아 있다(pong 도 last_frame_at 을 갱신).
                    # 이전에는 텍스트 이벤트만 '응답'으로 세어 조용한 채널에서 150초마다 끊고 다시 붙었다.
                    if ws.idle_seconds() > self.idle_limit:
                        raise WebSocketError("%d초 동안 응답이 없습니다." % self.idle_limit)
                    ws.ping()
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                post = should_handle(event, self.me["id"], self.me["username"])
                if post:
                    try:
                        self.on_post(post)
                    except Exception:
                        log.exception("글 접수 실패")
        finally:
            ws.close()

    def run(self):
        self.me = self.client.get_me()
        log.info("봇 계정: @%s (%s)", self.me["username"], self.me["id"])
        self.write_status(bot=self.me["username"], started_at=time.strftime("%Y-%m-%dT%H:%M:%S"), connected=False)
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.cleanup_loop, daemon=True).start()
        delay = 2
        while True:
            try:
                self.listen_once()
                delay = 2
            except (WebSocketError, MattermostError, OSError) as e:
                log.warning("연결 끊김: %s — %d초 후 재연결", e, delay)
                self.write_status(connected=False, last_error=str(e))
                time.sleep(delay)
                delay = min(delay * 2, 60)

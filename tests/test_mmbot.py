"""Mattermost 봇(src/mmbot, scripts/run_bot.py) 자동 테스트. 실제 Mattermost 없이 로컬 가짜 서버로 확인한다."""

import base64
import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mmbot import handler  # noqa: E402
from mmbot.bot import Bot, should_handle  # noqa: E402
from mmbot.client import MattermostClient, MattermostError, encode_multipart  # noqa: E402
from mmbot.websocket import (WebSocket, WebSocketClosed, WebSocketError, encode_frame, parse_frame, GUID,  # noqa: E402
                             OP_TEXT, OP_PING, OP_PONG, OP_CLOSE, OP_CONT)
from run_bot import load_env, settings, main as run_bot_main  # noqa: E402
from hwpx_fixture import build_fixture  # noqa: E402


def server_frame(opcode, payload, fin=True):
    """서버→클라이언트 프레임(마스킹 없음)."""
    n = len(payload)
    head = bytes([(0x80 if fin else 0) | opcode])
    head += bytes([n]) if n < 126 else bytes([126]) + n.to_bytes(2, "big")
    return head + payload


class FrameTest(unittest.TestCase):
    def test_roundtrip_masked_lengths(self):
        for n in (0, 5, 125, 126, 300, 70000):
            data = os.urandom(n)
            frame = encode_frame(OP_TEXT, data)
            (fin, op, payload), used = parse_frame(frame)
            self.assertEqual((fin, op, payload, used), (True, OP_TEXT, data, len(frame)))
            self.assertNotEqual(frame[-n:], data) if n > 8 else None

    def test_incomplete_frame_returns_none(self):
        frame = encode_frame(OP_TEXT, b"x" * 300)
        for cut in (1, 3, 7, len(frame) - 1):
            self.assertIsNone(parse_frame(frame[:cut]))
        self.assertIsNotNone(parse_frame(frame + b"extra"))


class FakeWsServer(threading.Thread):
    """핸드셰이크 → ping → 조각 텍스트 2개 → close 를 보내는 1회용 서버."""

    def __init__(self):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.pong = None
        self.auth = None

    def run(self):
        conn, _ = self.srv.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            req += conn.recv(4096)
        hdrs = dict(l.split(": ", 1) for l in req.decode().split("\r\n")[1:] if ": " in l)
        self.auth = hdrs.get("Authorization")
        acc = base64.b64encode(hashlib.sha1((hdrs["Sec-WebSocket-Key"] + GUID).encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      "Sec-WebSocket-Accept: %s\r\n\r\n" % acc).encode())
        conn.sendall(server_frame(OP_PING, b"hi"))
        buf = b""
        while parse_frame(buf) is None:
            buf += conn.recv(4096)
        self.pong = parse_frame(buf)[0]
        msg = json.dumps({"event": "hello", "text": "한글"}).encode()
        conn.sendall(server_frame(OP_TEXT, msg[:5], fin=False))
        time.sleep(0.05)
        conn.sendall(server_frame(OP_CONT, msg[5:]))
        conn.sendall(server_frame(OP_CLOSE, b""))
        time.sleep(0.2)
        conn.close()
        self.srv.close()


class WebSocketClientTest(unittest.TestCase):
    def test_handshake_ping_fragments_close(self):
        srv = FakeWsServer()
        srv.start()
        ws = WebSocket("ws://127.0.0.1:%d/api/v4/websocket" % srv.port, {"Authorization": "Bearer t"}, timeout=5)
        self.assertEqual(json.loads(ws.recv_text()), {"event": "hello", "text": "한글"})
        self.assertEqual(srv.pong[1:], (OP_PONG, b"hi"))
        self.assertEqual(srv.auth, "Bearer t")
        with self.assertRaises(WebSocketClosed):
            ws.recv_text()
        ws.close()
        srv.join(2)


class QuietWsServer(threading.Thread):
    """이벤트를 보내지 않는 조용한 서버. 클라이언트 ping 에 pong 으로 답하고(answer_pings), 일정 시간 뒤 close 를 보낸다."""

    def __init__(self, answer_pings, close_after):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.answer_pings, self.close_after = answer_pings, close_after
        self.pings = 0

    def run(self):
        conn, _ = self.srv.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            req += conn.recv(4096)
        hdrs = dict(l.split(": ", 1) for l in req.decode().split("\r\n")[1:] if ": " in l)
        acc = base64.b64encode(hashlib.sha1((hdrs["Sec-WebSocket-Key"] + GUID).encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      "Sec-WebSocket-Accept: %s\r\n\r\n" % acc).encode())
        conn.settimeout(0.05)
        end = time.time() + self.close_after
        buf = b""
        while time.time() < end:
            try:
                buf += conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            while True:
                got = parse_frame(buf)
                if not got:
                    break
                (fin, op, payload), used = got
                buf = buf[used:]
                if op == OP_PING:
                    self.pings += 1
                    if self.answer_pings:
                        conn.sendall(server_frame(OP_PONG, payload))
        try:
            conn.sendall(server_frame(OP_CLOSE, b""))
        except OSError:
            pass
        time.sleep(0.1)
        conn.close()
        self.srv.close()


class QuietConnectionTest(unittest.TestCase):
    """이벤트가 없는 조용한 채널에서도 pong 이 오면 연결을 유지해야 한다(배포 후 150초마다 끊기던 버그)."""

    def make_bot(self, port, tmp):
        class C:
            def websocket(self, timeout):
                return WebSocket("ws://127.0.0.1:%d/api/v4/websocket" % port, timeout=timeout)
        bot = Bot(C(), tmp, ws_timeout=0.1, idle_limit=0.5)
        bot.me = {"id": "b1", "username": "letshwp"}
        return bot

    def test_pong_keeps_connection_alive(self):
        srv = QuietWsServer(answer_pings=True, close_after=1.5)
        srv.start()
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(srv.port, tmp)
            t0 = time.time()
            with self.assertRaises(WebSocketClosed):   # idle 한도(0.5초)를 훨씬 넘겨도 서버가 끊을 때까지 유지
                bot.listen_once()
            self.assertGreater(time.time() - t0, 1.2)
        self.assertGreaterEqual(srv.pings, 5)
        srv.join(2)

    def test_no_pong_disconnects_after_idle_limit(self):
        srv = QuietWsServer(answer_pings=False, close_after=5)
        srv.start()
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(srv.port, tmp)
            t0 = time.time()
            with self.assertRaisesRegex(WebSocketError, "응답이 없습니다"):
                bot.listen_once()
            self.assertLess(time.time() - t0, 3)
        srv.join(6)

    def test_pong_frame_updates_last_frame_at(self):
        ws = WebSocket.__new__(WebSocket)
        ws._buf, ws._parts, ws.last_frame_at = server_frame(OP_PONG, b"") + server_frame(OP_TEXT, b"{}"), [], 0
        ws.sock = None
        self.assertEqual(ws.recv_text(), "{}")
        self.assertGreater(ws.last_frame_at, 0)
        self.assertLess(ws.idle_seconds(), 1)


class FakeApi(BaseHTTPRequestHandler):
    posts = []
    uploads = []

    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer good":
            return self._send(401, {"message": "invalid token"})
        if self.path == "/api/v4/users/me":
            return self._send(200, {"id": "bot1", "username": "letshwp", "is_bot": True})
        self._send(404, {})

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path == "/api/v4/files":
            FakeApi.uploads.append((self.headers["Content-Type"], body))
            return self._send(201, {"file_infos": [{"id": "f%d" % len(FakeApi.uploads)}]})
        if self.path == "/api/v4/posts":
            FakeApi.posts.append(json.loads(body))
            return self._send(201, {"id": "p1"})
        self._send(404, {})


class ClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = HTTPServer(("127.0.0.1", 0), FakeApi)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_get_me_and_bad_token(self):
        self.assertEqual(MattermostClient(self.url, "good").get_me()["username"], "letshwp")
        with self.assertRaises(MattermostError) as cm:
            MattermostClient(self.url, "bad").get_me()
        self.assertEqual(cm.exception.status, 401)

    def test_upload_multipart_and_post(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "보고서_수정본.hwpx"
            p.write_bytes(b"PK\x03\x04data")
            c = MattermostClient(self.url, "good")
            fid = c.upload_file("ch1", str(p))
            c.create_post("ch1", "완료", root_id="r1", file_ids=[fid])
        ctype, body = FakeApi.uploads[-1]
        self.assertIn("multipart/form-data; boundary=", ctype)
        self.assertIn(b'name="channel_id"\r\n\r\nch1', body)
        self.assertIn(b"PK\x03\x04data", body)
        self.assertIn("filename*=UTF-8''%EB%B3%B4%EA%B3%A0%EC%84%9C".encode(), body)
        self.assertEqual(FakeApi.posts[-1], {"channel_id": "ch1", "message": "완료", "root_id": "r1",
                                             "file_ids": [fid]})

    def test_run_bot_check(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / "bot.env"
            env.write_text("MM_URL=%s\nMM_TOKEN=good\nLETSHWP_DATA_DIR=%s\n" % (self.url, d), encoding="utf-8")
            self.assertEqual(run_bot_main(["--env", str(env), "--check"]), 0)
            env.write_text("MM_URL=%s\nMM_TOKEN=bad\nLETSHWP_DATA_DIR=%s\n" % (self.url, d), encoding="utf-8")
            self.assertEqual(run_bot_main(["--env", str(env), "--check"]), 2)

    def test_encode_multipart_fields(self):
        body, ctype = encode_multipart({"a": "1"}, [("files", 'x"y.hwpx', b"z")])
        self.assertTrue(body.endswith(b"--\r\n"))
        self.assertIn(b'filename="x_y.hwpx"', body)


class ParseRequestTest(unittest.TestCase):
    def test_modes(self):
        p = handler.parse_request
        self.assertEqual(p("")["mode"], "fix")
        self.assertEqual(p("@letshwp", "letshwp")["mode"], "fix")
        self.assertEqual(p("수정해줘")["mode"], "fix")
        self.assertEqual(p("검사만 해줘")["mode"], "test")
        self.assertEqual(p("테스트")["mode"], "test")
        self.assertEqual(p("검사하고 고쳐줘")["mode"], "fix")
        self.assertEqual(p("도움말")["mode"], "help")
        self.assertEqual(p("?")["mode"], "help")

    def test_doc_type(self):
        p = handler.parse_request
        self.assertEqual(p("수정 A")["doc_type"], "A")
        self.assertEqual(p("검사 type b")["doc_type"], "B")
        self.assertEqual(p("B형으로 수정")["doc_type"], "B")
        self.assertEqual(p("유형 A")["doc_type"], "A")
        self.assertEqual(p("--doc-type B")["doc_type"], "B")
        self.assertIsNone(p("A4 용지로 수정")["doc_type"])
        self.assertIsNone(p("수정해줘")["doc_type"])
        self.assertIsNone(p("@ab 수정", "ab")["doc_type"])

    def test_safe_filename(self):
        self.assertEqual(handler.safe_filename("../../etc/보고서.hwpx"), "보고서.hwpx")
        self.assertEqual(handler.safe_filename("a\\b\\c.hwpx"), "c.hwpx")
        self.assertEqual(handler.safe_filename("..hwpx"), "hwpx")
        self.assertEqual(handler.safe_filename(""), "document.hwpx")
        import unicodedata
        nfd = unicodedata.normalize("NFD", "보고서.hwpx")
        self.assertNotEqual(nfd, "보고서.hwpx")
        self.assertEqual(handler.safe_filename(nfd), "보고서.hwpx")


def posted(post, channel_type="O", mentions=None):
    data = {"post": json.dumps(post), "channel_type": channel_type}
    if mentions is not None:
        data["mentions"] = json.dumps(mentions)
    return {"event": "posted", "data": data}


class ShouldHandleTest(unittest.TestCase):
    def test_rules(self):
        base = {"id": "p", "user_id": "u1", "channel_id": "c", "message": "수정", "type": ""}
        self.assertTrue(should_handle(posted(base, "D"), "bot", "letshwp"))
        self.assertIsNone(should_handle(posted(base, "O"), "bot", "letshwp"))
        self.assertTrue(should_handle(posted(base, "O", ["bot"]), "bot", "letshwp"))
        self.assertTrue(should_handle(posted(dict(base, message="@LetsHWP 검사"), "O"), "bot", "letshwp"))
        self.assertIsNone(should_handle(posted(dict(base, user_id="bot"), "D"), "bot", "letshwp"))
        self.assertIsNone(should_handle(posted(dict(base, type="system_join_channel"), "D"), "bot", "letshwp"))
        self.assertIsNone(should_handle(posted(dict(base, props={"from_bot": "true"}), "D"), "bot", "letshwp"))
        self.assertIsNone(should_handle({"event": "typing", "data": {}}, "bot", "letshwp"))


class FakeClient:
    def __init__(self, files):
        self.files = files  # id -> (info, bytes)
        self.posts = []
        self.uploaded = []

    def get_me(self):
        return {"id": "bot", "username": "letshwp"}

    def get_file_info(self, fid):
        return self.files[fid][0]

    def download_file(self, fid):
        return self.files[fid][1]

    def upload_file(self, channel_id, path, filename=None):
        self.uploaded.append(Path(path).name)
        return "up%d" % len(self.uploaded)

    def create_post(self, channel_id, message, root_id=None, file_ids=None):
        self.posts.append({"message": message, "root_id": root_id, "file_ids": file_ids or []})


class BotFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        src = self.dir / "doc.hwpx"
        build_fixture(src)
        self.data = src.read_bytes()
        self.client = FakeClient({
            "f1": ({"id": "f1", "name": "보고서.hwpx", "extension": "hwpx", "size": len(self.data)}, self.data),
            "f2": ({"id": "f2", "name": "사진.png", "extension": "png", "size": 3}, b"png"),
        })
        self.bot = Bot(self.client, self.dir / "data")
        self.bot.me = self.client.get_me()

    def tearDown(self):
        self.tmp.cleanup()

    def drain(self):
        while not self.bot.jobs.empty():
            self.bot.process(*self.bot.jobs.get())

    def post(self, message, file_ids):
        return {"id": "post1", "root_id": "", "channel_id": "c1", "message": message, "file_ids": file_ids}

    def test_fix_flow_replies_with_fixed_file_only(self):
        self.bot.on_post(self.post("@letshwp 수정", ["f1", "f2"]))
        self.assertIn("접수했습니다", self.client.posts[0]["message"])
        self.drain()
        last = self.client.posts[-1]
        self.assertEqual(last["root_id"], "post1")
        self.assertRegex(last["message"], r"^\*\*서식 수정 완료\*\* — `보고서.hwpx` \(수정 \d+건\)")
        # 수정본 hwpx 하나만 첨부한다
        self.assertEqual(self.client.uploaded, ["보고서_수정본.hwpx"])
        # 검증 통과·적합 집계·코드 버전은 싣지 않는다
        for word in ("검증", "통과", "PASS", "적합", "코드 버전", "원본 변경"):
            self.assertNotIn(word, last["message"])
        # 수정한 내용은 보고서 2절 그대로 옮긴다
        report = (next((self.dir / "data" / "output").glob("*_수정")) / "change_report.md").read_text(encoding="utf-8")
        first_row = next(l for l in report.split("## 2.")[1].splitlines() if l.startswith("| 문단"))
        self.assertIn(first_row, last["message"])
        # 원본 첨부는 data/inbox 에 그대로 보관된다
        saved = next((self.dir / "data" / "inbox").glob("*/보고서.hwpx"))
        self.assertEqual(saved.read_bytes(), self.data)

    def test_test_flow_with_doc_type(self):
        self.bot.on_post(self.post("검사 B", ["f1"]))
        self.assertIn("(TYPE B)", self.client.posts[0]["message"])
        self.drain()
        last = self.client.posts[-1]
        self.assertIn("**서식 검사 완료**", last["message"])
        self.assertIn("①", last["message"])
        self.assertEqual(self.client.uploaded, ["check_report.txt"])
        info = json.loads(next((self.dir / "data" / "output").glob("*/run_info.json")).read_text(encoding="utf-8"))
        self.assertEqual(info["doc_type"], "B")

    def test_no_hwpx_gets_help(self):
        self.bot.on_post(self.post("수정", ["f2"]))
        self.assertIn("hwpx 파일이 첨부되지 않았습니다", self.client.posts[0]["message"])
        self.assertTrue(self.bot.jobs.empty())

    def test_too_large_file_rejected(self):
        self.bot.max_bytes = 10
        self.bot.on_post(self.post("", ["f1"]))
        self.assertIn("넘어서 처리하지 않습니다", self.client.posts[0]["message"])
        self.assertTrue(self.bot.jobs.empty())

    def test_broken_file_reports_failure_without_output(self):
        self.client.files["f1"] = (self.client.files["f1"][0], b"not a zip")
        self.bot.on_post(self.post("", ["f1"]))
        self.drain()
        last = self.client.posts[-1]
        self.assertIn("실패", last["message"])
        self.assertEqual(self.client.uploaded, [])

    def test_status_file(self):
        self.bot.write_status(connected=True)
        st = json.loads((self.dir / "data" / "status.json").read_text(encoding="utf-8"))
        self.assertTrue(st["connected"])


REPORT_MD = """# 서식 수정 보고서

> ✅ **수정 완료 · 검증 통과** — 수정본: `a_수정본.hwpx`

## 1. 한눈에 보기

| 구분 | 수정 전 | 수정 후 |
|---|---|---|
| 적합 | 1 | 3 |

## 2. 수정한 내용

### 자간 — 2건

| 위치 | 내용 | 변경 전 | 변경 후 |
|---|---|---|---|
| 문단 #1 (sec0/p0) | 가 | -4% | 0% |
| 문단 #2 (sec0/p1) | 나 | -4% | 0% |

## 3. 검증 결과

| 검증 항목 | 결과 | 상세 |
|---|---|---|
| 패키지 | ✅ 통과 | ok |

## 4. 수정하지 않은 항목 (직접 확인 필요)

{manual}
- 판단 불가 1건·검사 제외 0건은 기준이 정해지지 않았거나 역할이 불확실해 수정하지 않았습니다(검사 보고서 ④절).

## 5. 항목별 재검사 비교

| 검사 항목 | 수정 필요 | 적합 | 판단 불가 |
|---|---|---|---|
| 자간 | 2 → 0 | 1 → 3 | 0 → 0 |

## 6. 참고

- 원본 파일은 열기만 했고, 수정은 복사본에만 적용했습니다.
- 자간·줄 간격·문단 간격이 바뀌면 줄 나눔과 쪽 나눔이 달라질 수 있습니다. 한글에서 열어 화면 배치를 확인하세요.
"""

MANUAL = """### 검사만 하는 항목 — 본문 글자를 바꿔야 하므로 자동 수정하지 않음

| 항목 | 위치 | 내용 | 현재 | 표준 |
|---|---|---|---|---|
| 날짜 | 문단 #3 (sec0/p2) | 2026.1.1 | 2026.1.1 | 2026. 1. 1. |
"""


class FixMessageTest(unittest.TestCase):
    def test_changes_only_no_pass_info(self):
        m = handler.fix_message("a.hwpx", REPORT_MD.format(manual="없습니다.\n"))
        self.assertTrue(m.startswith("**서식 수정 완료** — `a.hwpx` (수정 2건)"))
        self.assertIn("#### 자간 — 2건", m)
        self.assertIn("| 문단 #2 (sec0/p1) | 나 | -4% | 0% |", m)
        self.assertIn("줄 나눔과 쪽 나눔", m)
        for word in ("검증", "통과", "적합", "한눈에", "재검사", "직접 확인", "판단 불가", "원본 파일은"):
            self.assertNotIn(word, m)
        self.assertNotIn("\n\n\n", m)

    def test_manual_review_kept(self):
        m = handler.fix_message("a.hwpx", REPORT_MD.format(manual=MANUAL))
        self.assertIn("#### 직접 확인 필요", m)
        self.assertIn("| 날짜 | 문단 #3 (sec0/p2) | 2026.1.1 | 2026.1.1 | 2026. 1. 1. |", m)

    def test_long_report_falls_back_to_counts(self):
        m = handler.fix_message("a.hwpx", REPORT_MD.format(manual="없습니다.\n"), limit=200)
        self.assertIn("- 자간 — 2건", m)
        self.assertNotIn("| 문단 #1", m)
        self.assertIn("항목별 건수만", m)

    def test_nothing_changed(self):
        md = REPORT_MD.format(manual="없습니다.\n").split("## 2.")[0] + "## 2. 수정한 내용\n\n수정한 항목이 없습니다.\n"
        m = handler.fix_message("a.hwpx", md)
        self.assertIn("바꾼 것이 없습니다", m)
        self.assertNotIn("줄 나눔", m)


class HandlerMiscTest(unittest.TestCase):
    def test_truncate(self):
        self.assertEqual(handler.truncate("abc", 10), "abc")
        self.assertIn("여기까지만", handler.truncate("a" * 100, 10))

    def test_cleanup_old(self):
        with tempfile.TemporaryDirectory() as d:
            old = Path(d) / "output" / "old"
            new = Path(d) / "inbox" / "new"
            old.mkdir(parents=True)
            new.mkdir(parents=True)
            t = time.time() - 40 * 86400
            os.utime(old, (t, t))
            self.assertEqual(handler.cleanup_old(d, 30), 1)
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())
            self.assertEqual(handler.cleanup_old(d, 0), 0)


class RunBotConfigTest(unittest.TestCase):
    def test_load_env_and_settings(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bot.env"
            p.write_text("# 주석\nMM_URL='http://x'\nMM_TOKEN=\"t=1\"\n\nLETSHWP_DATA_DIR=/d\nMM_VERIFY_TLS=false\n",
                         encoding="utf-8")
            env = load_env(p)
        self.assertEqual(env["MM_TOKEN"], "t=1")
        cfg = settings(env)
        self.assertEqual((cfg["url"], cfg["max_file_mb"], cfg["keep_days"], cfg["verify_tls"]),
                         ("http://x", 50, 30, False))
        with self.assertRaises(ValueError):
            settings({"MM_URL": "x"})

    def test_missing_settings_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bot.env"
            p.write_text("MM_URL=http://x\n", encoding="utf-8")
            old = {k: os.environ.pop(k) for k in ("MM_TOKEN", "LETSHWP_DATA_DIR") if k in os.environ}
            try:
                self.assertEqual(run_bot_main(["--env", str(p), "--check"]), 2)
            finally:
                os.environ.update(old)


if __name__ == "__main__":
    unittest.main()

"""최소 WebSocket 클라이언트(RFC 6455). Mattermost 이벤트 수신용으로 텍스트 프레임만 다룬다.

외부 패키지 없이 socket + ssl 로 구현한다. 서버→클라이언트 프레임은 마스킹되지 않고,
클라이언트→서버 프레임은 반드시 마스킹한다.
"""

import base64
import hashlib
import os
import socket
import ssl
import struct
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketError(RuntimeError):
    pass


class WebSocketClosed(WebSocketError):
    pass


def encode_frame(opcode, payload, mask_key=None):
    """클라이언트 프레임(FIN=1, 마스킹)을 만든다."""
    mask_key = mask_key if mask_key is not None else os.urandom(4)
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack("!H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack("!Q", n)
    masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return head + mask_key + masked


def parse_frame(buf):
    """buf 앞의 프레임 하나를 해석한다. 완전한 프레임이면 ((fin, opcode, payload), 사용한 바이트 수),
    아직 덜 받았으면 None. 덜 받은 데이터는 버퍼에 그대로 남으므로 수신 시간 초과에도 안전하다."""
    if len(buf) < 2:
        return None
    b1, b2 = buf[0], buf[1]
    fin, opcode = bool(b1 & 0x80), b1 & 0x0F
    masked, n = bool(b2 & 0x80), b2 & 0x7F
    pos = 2
    if n == 126:
        if len(buf) < 4:
            return None
        n, pos = struct.unpack("!H", buf[2:4])[0], 4
    elif n == 127:
        if len(buf) < 10:
            return None
        n, pos = struct.unpack("!Q", buf[2:10])[0], 10
    key = None
    if masked:
        if len(buf) < pos + 4:
            return None
        key, pos = buf[pos:pos + 4], pos + 4
    if len(buf) < pos + n:
        return None
    payload = bytes(buf[pos:pos + n])
    if key:
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return (fin, opcode, payload), pos + n


class WebSocket:
    def __init__(self, url, headers=None, timeout=30, ssl_context=None):
        parts = urlsplit(url)
        if parts.scheme not in ("ws", "wss"):
            raise WebSocketError("ws:// 또는 wss:// 주소가 아닙니다: %s" % url)
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        sock = socket.create_connection((parts.hostname, port), timeout=timeout)
        if parts.scheme == "wss":
            sock = (ssl_context or ssl.create_default_context()).wrap_socket(sock, server_hostname=parts.hostname)
        self.sock = sock
        self._buf = b""
        self._parts = []
        key = base64.b64encode(os.urandom(16)).decode()
        path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        host = parts.hostname + ("" if parts.port is None else ":%d" % parts.port)
        lines = ["GET %s HTTP/1.1" % path, "Host: %s" % host, "Upgrade: websocket", "Connection: Upgrade",
                 "Sec-WebSocket-Key: %s" % key, "Sec-WebSocket-Version: 13"]
        lines += ["%s: %s" % kv for kv in (headers or {}).items()]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        while b"\r\n\r\n" not in self._buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketError("핸드셰이크 중 연결이 끊겼습니다.")
            self._buf += chunk
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        status, *hdrs = head.decode("latin-1").split("\r\n")
        if " 101 " not in status + " ":
            raise WebSocketError("WebSocket 연결 거부: %s" % status)
        accept = {k.strip().lower(): v.strip() for k, v in (h.split(":", 1) for h in hdrs if ":" in h)}
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if accept.get("sec-websocket-accept") != expected:
            raise WebSocketError("Sec-WebSocket-Accept 값이 맞지 않습니다.")

    def _next_frame(self):
        while True:
            got = parse_frame(self._buf)
            if got:
                frame, used = got
                self._buf = self._buf[used:]
                return frame
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocketClosed("서버가 연결을 끊었습니다.")
            self._buf += chunk

    def send_text(self, text):
        self.sock.sendall(encode_frame(OP_TEXT, text.encode("utf-8")))

    def ping(self):
        self.sock.sendall(encode_frame(OP_PING, b""))

    def recv_text(self):
        """텍스트 메시지 하나를 돌려준다. ping 에는 pong 으로 답한다. 시간 초과 시 socket.timeout."""
        while True:
            fin, opcode, payload = self._next_frame()
            if opcode == OP_PING:
                self.sock.sendall(encode_frame(OP_PONG, payload))
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                raise WebSocketClosed("서버가 연결 종료 프레임을 보냈습니다.")
            if opcode in (OP_TEXT, OP_BINARY, OP_CONT):
                # 조각 프레임은 메시지가 끝날 때까지 모은다(시간 초과 후에도 이어 받을 수 있게 인스턴스에 보관)
                self._parts.append(payload)
                if fin:
                    msg, self._parts = b"".join(self._parts), []
                    return msg.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.sendall(encode_frame(OP_CLOSE, b""))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

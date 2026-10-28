"""Mattermost REST API v4 클라이언트(urllib). 봇에 필요한 호출만 둔다.

근거: Mattermost API v4 — /users/me, /files, /files/{id}, /files/{id}/info, /posts, /websocket
"""

import json
import os
import ssl
import uuid
from urllib import request, error
from urllib.parse import urlsplit, quote

from .websocket import WebSocket


class MattermostError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def encode_multipart(fields, files):
    """fields: {이름: 값}, files: [(필드명, 파일명, bytes)] → (body, content_type)."""
    boundary = "----letshwp" + uuid.uuid4().hex
    out = []
    for name, value in fields.items():
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                    % (boundary, name, value)).encode("utf-8"))
    for field, filename, data in files:
        # 한글 파일명은 RFC 5987 filename* 로도 함께 보낸다
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"; filename*=UTF-8''%s\r\n"
                    "Content-Type: application/octet-stream\r\n\r\n"
                    % (boundary, field, filename.replace('"', "_"), quote(filename))).encode("utf-8"))
        out.append(data)
        out.append(b"\r\n")
    out.append(("--%s--\r\n" % boundary).encode())
    return b"".join(out), "multipart/form-data; boundary=%s" % boundary


class MattermostClient:
    def __init__(self, base_url, token, timeout=60, verify_tls=True):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.ssl_context = ssl.create_default_context() if verify_tls else ssl._create_unverified_context()

    def _request(self, method, path, body=None, content_type=None, raw=False):
        headers = {"Authorization": "Bearer %s" % self.token}
        data = None
        if body is not None:
            if isinstance(body, (bytes, bytearray)):
                data = bytes(body)
            else:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                content_type = content_type or "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        req = request.Request(self.base_url + "/api/v4" + path, data=data, headers=headers, method=method)
        try:
            ctx = self.ssl_context if self.base_url.startswith("https") else None
            with request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                payload = resp.read()
        except error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise MattermostError("%s %s 실패(%d): %s" % (method, path, e.code, detail), e.code) from None
        except (error.URLError, OSError) as e:
            raise MattermostError("%s %s 연결 실패: %s" % (method, path, e)) from None
        if raw:
            return payload
        return json.loads(payload.decode("utf-8")) if payload else None

    def get_me(self):
        return self._request("GET", "/users/me")

    def get_file_info(self, file_id):
        return self._request("GET", "/files/%s/info" % file_id)

    def download_file(self, file_id):
        return self._request("GET", "/files/%s" % file_id, raw=True)

    def upload_file(self, channel_id, path, filename=None):
        filename = filename or os.path.basename(path)
        with open(path, "rb") as f:
            data = f.read()
        body, ctype = encode_multipart({"channel_id": channel_id}, [("files", filename, data)])
        res = self._request("POST", "/files", body, ctype)
        return res["file_infos"][0]["id"]

    def create_post(self, channel_id, message, root_id=None, file_ids=None):
        body = {"channel_id": channel_id, "message": message}
        if root_id:
            body["root_id"] = root_id
        if file_ids:
            body["file_ids"] = list(file_ids)
        return self._request("POST", "/posts", body)

    def websocket(self, timeout=30):
        parts = urlsplit(self.base_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        url = "%s://%s%s/api/v4/websocket" % (scheme, parts.netloc, parts.path)
        return WebSocket(url, {"Authorization": "Bearer %s" % self.token}, timeout=timeout,
                         ssl_context=self.ssl_context)

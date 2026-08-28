#!/usr/bin/env python3
"""
Right Code (rightapi.ai/draw) 异步中转 → 同步 OpenAI 兼容代理
- Listen: 127.0.0.1:8090
- POST /v1/images/generations  (OpenAI 同步格式)
- POST /v1/images/edits        (with mask, multipart 简化)
- GET  /healthz
- 内部:转发到 rightapi.ai/draw/v1/images/generations (async=true),轮询 /v1/tasks/{id}
- 返回:标准 OpenAI {created, data:[{url}]}
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import base64
import tempfile
import cgi
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "https://www.rightapi.ai")
UPSTREAM_KEY = os.environ.get("UPSTREAM_KEY", "")
LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8090"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.5"))
POLL_MAX_SECS = float(os.environ.get("POLL_MAX_SECS", "180"))


def log(msg):
    sys.stdout.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    sys.stdout.flush()


def _http_call(method, path, body, retries=3):
    url = f"{UPSTREAM_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {
        "Authorization": f"Bearer {UPSTREAM_KEY}",
        "User-Agent": "curl/8.5.0",
        "Accept": "application/json",
    }
    if data is not None:
        h["Content-Type"] = "application/json"
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=h, method=method)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last_err = e
            log(f"  retry {attempt+1}/{retries} after {type(e).__name__}: {e}")
            time.sleep(1 + attempt)
    raise last_err


def upstream_post(path, body, headers=None):
    h_extra = headers or {}
    # merge into _http_call by stashing extra headers via closure on body
    # simpler: rebuild here with extra headers
    url = f"{UPSTREAM_BASE}{path}"
    data = json.dumps(body).encode("utf-8")
    h = {
        "Authorization": f"Bearer {UPSTREAM_KEY}",
        "User-Agent": "curl/8.5.0",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    h.update(h_extra)
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data, headers=h, method="POST")
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last_err = e
            log(f"  POST retry {attempt+1}/3 after {type(e).__name__}: {e}")
            time.sleep(1 + attempt)
    raise last_err


def upstream_get(path):
    url = f"{UPSTREAM_BASE}{path}"
    h = {
        "Authorization": f"Bearer {UPSTREAM_KEY}",
        "User-Agent": "curl/8.5.0",
        "Accept": "application/json",
    }
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=h, method="GET")
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last_err = e
            log(f"  GET retry {attempt+1}/3 after {type(e).__name__}: {e}")
            time.sleep(1 + attempt)
    raise last_err


def wait_for_task(task_id):
    """轮询直到 status 进入终态(我们的中转无 status 字段,只用 created/data 判断完成)"""
    deadline = time.time() + POLL_MAX_SECS
    while time.time() < deadline:
        try:
            code, body = upstream_get(f"/v1/tasks/{task_id}")
            obj = json.loads(body)
            # 兼容字段:completed / success / done; 或者有 data 就算完成
            status = (obj.get("status") or "").lower()
            if status in ("completed", "succeeded", "success", "done", "finished"):
                return obj
            if "data" in obj and obj["data"]:
                return obj
            if status in ("failed", "error", "canceled"):
                raise RuntimeError(f"task failed: {obj}")
        except Exception as e:
            log(f"  poll err: {e}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"task {task_id} timeout after {POLL_MAX_SECS}s")


def build_oai_response(task_obj, model):
    """把中转的 {created, data:[{url}]} 转成 OpenAI 标准 {created, data:[{url, ...}]}"""
    urls = []
    for d in task_obj.get("data", []):
        if "url" in d:
            urls.append({"url": d["url"]})
        elif "b64_json" in d:
            urls.append({"b64_json": d["b64_json"]})
    return {
        "created": task_obj.get("created", int(time.time())),
        "data": urls,
        "model": model,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log(f"  http: {self.address_string()} - {fmt % args}")

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/healthz", "/health", "/"):
            self._send_json(200, {"ok": True, "upstream": UPSTREAM_BASE, "listen": f"{LISTEN_HOST}:{LISTEN_PORT}"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/v1/images/generations":
            self._handle_generate()
        elif self.path == "/v1/images/edits":
            self._handle_edit()
        else:
            self._send_json(404, {"error": f"unsupported path: {self.path}"})

    def _read_body_json(self):
        n = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"invalid json body: {e}")

    def _handle_generate(self):
        try:
            req = self._read_body_json()
        except ValueError as e:
            return self._send_json(400, {"error": str(e)})
        model = req.get("model", "gpt-image-2")
        prompt = req.get("prompt", "")
        if not prompt:
            return self._send_json(400, {"error": {"message": "prompt is required", "type": "invalid_request_error"}})
        n = int(req.get("n", 1))
        size = req.get("size", "1024x1024")
        log(f"generate: model={model} n={n} size={size} prompt={prompt[:60]!r}")
        # 中转只支持单张,但允许多次提交
        results = []
        for i in range(n):
            upstream_body = {
                "model": model,
                "prompt": prompt,
                "size": size,
                "async": True,
            }
            code, body = upstream_post("/draw/v1/images/generations", upstream_body)
            obj = json.loads(body)
            task_id = obj.get("task_id")
            if not task_id:
                return self._send_json(500, {"error": {"message": f"upstream no task_id: {body[:300]}"}})
            log(f"  submitted task {i+1}/{n}: {task_id}")
            task_obj = wait_for_task(task_id)
            log(f"  done task {task_id}: {task_obj}")
            for d in task_obj.get("data", []):
                if "url" in d:
                    results.append({"url": d["url"]})
                elif "b64_json" in d:
                    results.append({"b64_json": d["b64_json"]})
        self._send_json(200, {
            "created": int(time.time()),
            "data": results,
            "model": model,
        })

    def _handle_edit(self):
        """OpenAI /v1/images/edits multipart -> upstream /draw/v1/images/edits
        支持 image / mask / prompt / model / n / size
        """
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/"):
            return self._send_json(400, {"error": {"message": "expected multipart/form-data", "type": "invalid_request_error"}})
        n = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(n) if n else b""
        env = {"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype, "CONTENT_LENGTH": str(n)}
        fs = cgi.FieldStorage(fp=io.BytesIO(raw), environ=env, keep_blank_values=True)
        prompt = (fs.getfirst("prompt") or "").strip()
        model = fs.getfirst("model") or "gpt-image-2"
        if not prompt:
            return self._send_json(400, {"error": {"message": "prompt is required", "type": "invalid_request_error"}})
        size = fs.getfirst("size") or "1024x1024"
        try:
            n_count = int(fs.getfirst("n") or "1")
        except ValueError:
            n_count = 1
        image_field = fs["image"] if "image" in fs else None
        mask_field = fs["mask"] if "mask" in fs else None
        if image_field is None and not fs.list:
            return self._send_json(400, {"error": {"message": "image is required for edit", "type": "invalid_request_error"}})
        log(f"edit: model={model} n={n_count} size={size} has_image={image_field is not None} has_mask={mask_field is not None} prompt={prompt[:60]!r}")
        # 构建 multipart 转发到 upstream
        boundary = "----asyncproxy" + str(int(time.time() * 1000))
        parts = []
        def add_field(name, value):
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            parts.append(str(value).encode())
            parts.append(b"\r\n")
        def add_file(name, filename, data, content_type="application/octet-stream"):
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode())
            parts.append(f"Content-Type: {content_type}\r\n\r\n".encode())
            parts.append(data)
            parts.append(b"\r\n")
        add_field("model", model)
        add_field("prompt", prompt)
        add_field("size", size)
        add_field("async", "true")
        if image_field is not None:
            data = image_field.file.read() if hasattr(image_field.file, "read") else image_field.value
            fname = image_field.filename or "image.png"
            ctype = image_field.type or "image/png"
            add_file("image", fname, data, ctype)
        if mask_field is not None:
            data = mask_field.file.read() if hasattr(mask_field.file, "read") else mask_field.value
            fname = mask_field.filename or "mask.png"
            ctype = mask_field.type or "image/png"
            add_file("mask", fname, data, ctype)
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        # 转发
        url = f"{UPSTREAM_BASE}/draw/v1/images/edits"
        h = {
            "Authorization": f"Bearer {UPSTREAM_KEY}",
            "User-Agent": "curl/8.5.0",
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        last_err = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=body, headers=h, method="POST")
                with urllib.request.urlopen(req, timeout=300) as r:
                    raw_resp = r.read().decode("utf-8", errors="replace")
                    log(f"  upstream edit resp: {raw_resp[:200]}")
                    obj = json.loads(raw_resp)
                    # 检查是否是 task_id 模式(async 任务)
                    if "task_id" in obj and "data" not in obj:
                        task_id = obj["task_id"]
                        log(f"  async edit task: {task_id}, polling...")
                        task_obj = wait_for_task(task_id)
                        return self._send_json(200, build_oai_response(task_obj, model))
                    # 同步模式,直接返回
                    return self._send_json(200, {
                        "created": obj.get("created", int(time.time())),
                        "data": obj.get("data", []),
                        "model": model,
                    })
            except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
                last_err = e
                log(f"  edit retry {attempt+1}/3 after {type(e).__name__}: {e}")
                time.sleep(1 + attempt)
        return self._send_json(502, {"error": {"message": f"upstream error: {last_err}", "type": "upstream_error"}})


def main():
    if not UPSTREAM_KEY:
        print("ERROR: UPSTREAM_KEY env var required", file=sys.stderr)
        sys.exit(2)
    log(f"listen on {LISTEN_HOST}:{LISTEN_PORT} -> {UPSTREAM_BASE}")
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

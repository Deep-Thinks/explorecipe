"""
explorecipe · 本地 HTTP 服务

设计目标：
  - 浏览器只跟 localhost 通信，密钥仅存服务端 .env
  - 两个核心后端端点：
      POST /api/generate-explosion  上传一张食物图，转发到 dm-fox gpt-image-2，
                                    返回生成的爆炸分解图 PNG
      POST /api/explain-region      上传裁切后的成分图块，转发到 StepFun step-3.6
                                    视觉模式，返回 Markdown 科普卡
  - 静态：/  或  /index.html  -> 前端 SPA

参考：lilibear-world/server.py，但去掉了任务系统/限流/SQLite/缩略图（MVP 用不上）
"""

import base64
import datetime
import http.server
import io
import json
import mimetypes
import os
import re
import socket
import socketserver
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid


DOC_ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_ROOT = os.path.join(DOC_ROOT, "logs")
IMG_LOG_ROOT = os.path.join(LOG_ROOT, "images")
EVENT_LOG_PATH = os.path.join(LOG_ROOT, "events.jsonl")


# ============================================================
# .env 加载（与 lilibear 一致：极简 K=V 行解析）
# ============================================================
def _load_env():
    for fname in (".env.local", ".env"):
        env_path = os.path.join(DOC_ROOT, fname)
        if not os.path.exists(env_path):
            continue
        with open(env_path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()
PORT = int(os.environ.get("PORT", "18081"))
BIND_HOST = os.environ.get("BIND_HOST", "::")
IMAGE_API_KEY = os.environ.get("IMAGE_API_KEY", "")
STEPFUN_API_KEY = os.environ.get("STEPFUN_API_KEY", "")
STEPFUN_BASE_URL = os.environ.get("STEPFUN_BASE_URL", "https://api.stepfun.com/v1")
STEPFUN_MODEL = os.environ.get("STEPFUN_MODEL", "step-3.6")

UPSTREAM_TIMEOUT = 600                       # 生成图最长等 10 分钟
MAX_UPLOAD_BYTES = 12 * 1024 * 1024          # 用户上传图上限 12MB
MAX_REGION_BYTES = 4 * 1024 * 1024           # 点击成分图上限 4MB
RETRY_TIMES = 3                              # gpt-image-2 间歇性"无输出"，沿用 lilibear 重试策略
RETRY_BACKOFF_BASE = 2.0

IMAGE_UPSTREAM_URL = os.environ.get("IMAGE_UPSTREAM_URL",
                                    "https://chatgpt2api2.zeabur.app/v1/images/edits")
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-2")
# /v1/images/edits 必须带 size；固定竖版爆炸图比例
IMAGE_SIZE = os.environ.get("IMAGE_SIZE", "1024x1536")


# ============================================================
# 通用工具
# ============================================================
def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _ensure_log_dirs():
    os.makedirs(LOG_ROOT, exist_ok=True)
    os.makedirs(IMG_LOG_ROOT, exist_ok=True)


_log_lock = threading.Lock()


def _write_event(payload):
    """事件落盘，单行 JSON。MVP 调试用。"""
    try:
        _ensure_log_dirs()
        payload = dict(payload)
        payload.setdefault("ts", _now_iso())
        line = json.dumps(payload, ensure_ascii=False)
        with _log_lock:
            with open(EVENT_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        print("[event] write failed: %s" % e)


def _detect_image_mime(data):
    """按字节签名判断图片 MIME。dm-fox 要求 multipart 里 Content-Type 真实，否则下游 vision 拒收。"""
    if not data or len(data) < 12:
        return "application/octet-stream"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _build_multipart(boundary, fields):
    """fields: list of (name, filename_or_None, bytes_value, content_type_or_None)。"""
    out = io.BytesIO()
    for item in fields:
        if len(item) == 3:
            name, filename, value = item
            ctype = None
        else:
            name, filename, value, ctype = item
        out.write(("--" + boundary + "\r\n").encode())
        if filename:
            out.write(
                ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                 % (name, filename)).encode()
            )
            out.write(
                ("Content-Type: %s\r\n\r\n" % (ctype or "application/octet-stream")).encode()
            )
        else:
            out.write(
                ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode()
            )
        if isinstance(value, str):
            value = value.encode("utf-8")
        out.write(value)
        out.write(b"\r\n")
    out.write(("--" + boundary + "--\r\n").encode())
    return out.getvalue()


# ============================================================
# Prompt 模板
# ============================================================
EXPLOSION_PROMPT = """Create a hyper-realistic VERTICAL exploded-view of the food in this image.

Layout rule (most important — must follow exactly):
Each ingredient MUST be its own SEPARATE LAYER stacked one above another along the VERTICAL axis (top to bottom). Each layer is centered horizontally. Adjacent layers MUST be separated by a clear 100-150 pixel vertical gap. NEVER spread ingredients horizontally side-by-side; NEVER place two ingredients at the same vertical height. Even if the original food is naturally horizontal or scattered (e.g., pizza laid flat, salad, charcuterie board, sushi platter with multiple pieces, plate with side dishes), you MUST mentally rearrange its components into a single VERTICAL stack column.

Duplicate rule:
If the original food contains multiple identical pieces of the same ingredient (e.g., 3 sushi pieces with the same fish, 2 dumplings, several scattered olives), COLLAPSE them into ONE representative layer — do NOT repeat the same ingredient as multiple layers.

Ingredient selection:
Identify 4-6 distinct ingredient categories (topping → main → base ordering). Keep it to 4-6 layers; don't fragment a single ingredient into sub-layers.

Background:
Preserve the original photo's background and ambient lighting — wooden table, plate edge, surrounding props, light direction. The scene should look like the food gracefully separated into floating ingredient layers in its own original environment.

Each ingredient layer:
- Photo-realistic with its natural colors and texture
- Soft drop shadow beneath it on the background
- Slight perspective consistent with the original photo's camera angle

Strict NO-GO list (must NOT appear in output):
- NO text labels, captions, or annotations
- NO indicator lines, arrows, callouts
- NO magenta / neon outlines around layers
- NO solid dark studio background — keep the ORIGINAL photo background

Studio-grade macro detail, sharp focus. Final image: a magical, clean, magazine-quality VERTICAL exploded view column.
"""


# ============================================================
# step-3.6 批量识别全图所有层（产品的核心智能 fetch）
# ============================================================
EXPLAIN_ALL_SYSTEM = """你是「食物解构师」。下方是一张食物的「垂直爆炸分解图」——食物的各成分被竖向分层悬浮展示。

请从上到下列出图中**所有可识别的成分层**（通常 3-7 层），为每一层输出：
- `index`: 从 0 开始的层序号（最上面是 0）
- `name_zh`: 中文成分名（简洁，2-6 字）
- `name_en`: 英文成分名
- `y_ratio_top`: 该层在图中**上边缘**的相对位置（0.0=顶 ~ 1.0=底，浮点）
- `y_ratio_bottom`: 该层**下边缘**的相对位置
- `card`: Markdown 科普卡，结构：开头一行 `## 中文名(EN)`，然后 **一句话本质** / **起源故事** / **营养亮点** / **趣味提示** 四段，每段 30-60 字。不要编精确营养数字。

**严格输出 JSON 数组**，外层就是数组本身，不要任何包裹文字、不要 markdown 代码块。例：

[
  {"index": 0, "name_zh": "面包顶", "name_en": "Brioche Bun Top",
   "y_ratio_top": 0.04, "y_ratio_bottom": 0.22,
   "card": "## 面包顶(Brioche Bun Top)\\n\\n**一句话本质**：..."},
  ...
]
"""


EXPLAIN_SYSTEM_PROMPT = """你是「食物解构师」，专门为用户讲解食物中各个成分的来历与营养。

用户从一张「食物爆炸分解图」上点击了一层，下方图块就是被点击的成分截图（可能带品红描边）。

请你：
1. 先识别这是什么成分（中文名 + 英文名）
2. 用 300 字以内中文写一段轻松有趣的科普卡，结构：
   - **一句话本质**：它是什么、来自哪种原料
   - **起源故事**：30-60 字的小故事或冷知识
   - **营养亮点**：1-2 个关键营养事实（数值不确定就说"约"，避免编造精确数字）
   - **趣味提示**：搭配建议、季节、文化意涵之一
3. 用 Markdown 输出，禁用 H1，可用 H2/H3 与简短列表。开头用 ## 中文名(EN) 作为标题。

如果图块完全无法识别（噪点 / 边缘失败），回复一句话："识别失败，换一处点点看吧～"
"""


# ============================================================
# 上游调用：dm-fox gpt-image-2（生成爆炸分解图）
# ============================================================
def call_dmfox_explosion(ref_image_bytes):
    """同步调用 dm-fox。返回 PNG 字节流。失败抛 RuntimeError。

    说明：按用户要求，不传 size 参数，让模型自选合适比例。
    """
    if not IMAGE_API_KEY:
        raise RuntimeError("IMAGE_API_KEY 未配置（检查 .env）")

    boundary = "----explorecipe-" + uuid.uuid4().hex
    ref_mime = _detect_image_mime(ref_image_bytes)
    if ref_mime == "application/octet-stream":
        raise RuntimeError("无法识别上传图片格式（仅支持 png/jpeg/webp/gif）")
    ref_ext = ref_mime.split("/")[-1]

    body = _build_multipart(boundary, [
        ("model", None, IMAGE_MODEL.encode("utf-8")),
        ("prompt", None, EXPLOSION_PROMPT.encode("utf-8")),
        ("size", None, IMAGE_SIZE.encode("utf-8")),
        ("n", None, b"1"),
        ("image", "food." + ref_ext, ref_image_bytes, ref_mime),
    ])

    headers = {
        "Authorization": "Bearer " + IMAGE_API_KEY,
        "Content-Type": "multipart/form-data; boundary=" + boundary,
        "Content-Length": str(len(body)),
    }

    ctx = ssl.create_default_context()
    last_err = None
    img_bytes = None
    total_t0 = time.time()
    for attempt in range(RETRY_TIMES):
        req = urllib.request.Request(IMAGE_UPSTREAM_URL, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=UPSTREAM_TIMEOUT) as resp:
                raw = resp.read()
            try:
                j = json.loads(raw.decode("utf-8"))
            except Exception:
                last_err = "上游响应非 JSON"
                continue
            if not j or "data" not in j or not j["data"]:
                last_err = "上游返回无图片数据：%s" % str(j)[:200]
                # 等一会儿再重试，dm-fox 这种"无输出"经验上是间歇性
                if attempt < RETRY_TIMES - 1:
                    time.sleep(RETRY_BACKOFF_BASE * (attempt + 1))
                continue
            item = j["data"][0]
            b64 = item.get("b64_json")
            url = item.get("url")
            if b64:
                img_bytes = base64.b64decode(b64)
            elif url:
                with urllib.request.urlopen(url, context=ctx, timeout=UPSTREAM_TIMEOUT) as ir:
                    img_bytes = ir.read()
            else:
                last_err = "上游返回格式未识别"
                continue
            break  # 成功
        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            last_err = "HTTP %d %s" % (e.code, body_text)
            if attempt < RETRY_TIMES - 1:
                time.sleep(RETRY_BACKOFF_BASE * (attempt + 1))
            continue
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
            if attempt < RETRY_TIMES - 1:
                time.sleep(RETRY_BACKOFF_BASE * (attempt + 1))
            continue

    if img_bytes is None:
        raise RuntimeError("dm-fox %d 次重试均失败：%s" % (RETRY_TIMES, last_err or "未知"))

    elapsed = round(time.time() - total_t0, 2)

    # 落盘存档
    try:
        _ensure_log_dirs()
        day = datetime.datetime.now().strftime("%Y%m%d")
        day_dir = os.path.join(IMG_LOG_ROOT, day)
        os.makedirs(day_dir, exist_ok=True)
        ext = ".png" if img_bytes[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"
        fname = datetime.datetime.now().strftime("%H%M%S") + "_" + uuid.uuid4().hex[:8] + ext
        with open(os.path.join(day_dir, fname), "wb") as f:
            f.write(img_bytes)
        _write_event({"type": "explosion_done", "elapsed": elapsed,
                      "bytes": len(img_bytes), "file": fname})
    except Exception as e:
        print("[archive] save failed: %s" % e)

    return img_bytes


# ============================================================
# 上游调用：StepFun step-3.6 视觉模式（写科普卡）
# ============================================================
def call_stepfun_explain(region_image_bytes):
    """同步调用 StepFun。返回 Markdown 字符串。"""
    if not STEPFUN_API_KEY:
        raise RuntimeError("STEPFUN_API_KEY 未配置（检查 .env）")

    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("缺少 openai 包：请 pip install -r requirements.txt")

    region_mime = _detect_image_mime(region_image_bytes)
    if region_mime == "application/octet-stream":
        region_mime = "image/png"

    b64 = base64.b64encode(region_image_bytes).decode("ascii")
    data_url = "data:%s;base64,%s" % (region_mime, b64)

    client = OpenAI(api_key=STEPFUN_API_KEY, base_url=STEPFUN_BASE_URL)
    t0 = time.time()
    resp = client.chat.completions.create(
        model=STEPFUN_MODEL,
        messages=[
            {"role": "system", "content": EXPLAIN_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": "这是用户点击的成分图块，请按系统指令生成科普卡。"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ],
        timeout=120,
    )
    elapsed = round(time.time() - t0, 2)
    content = resp.choices[0].message.content or ""
    _write_event({"type": "explain_done", "elapsed": elapsed, "chars": len(content)})
    return content


def call_stepfun_explain_all(full_image_bytes, dish_hint=None):
    """新核心：一次调用让 step-3.6 看整张爆炸图，返回所有层 JSON 数组。"""
    if not STEPFUN_API_KEY:
        raise RuntimeError("STEPFUN_API_KEY 未配置")
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("缺少 openai 包")

    mime = _detect_image_mime(full_image_bytes)
    if mime == "application/octet-stream":
        mime = "image/png"
    b64 = base64.b64encode(full_image_bytes).decode("ascii")
    data_url = "data:%s;base64,%s" % (mime, b64)

    client = OpenAI(api_key=STEPFUN_API_KEY, base_url=STEPFUN_BASE_URL)
    user_text = "列出图中所有成分层。"
    if dish_hint:
        user_text = f"这是「{dish_hint}」的爆炸分解图，列出所有层。"
    t0 = time.time()
    resp = client.chat.completions.create(
        model=STEPFUN_MODEL,
        messages=[
            {"role": "system", "content": EXPLAIN_ALL_SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ],
        timeout=300,
    )
    elapsed = round(time.time() - t0, 2)
    raw = (resp.choices[0].message.content or "").strip()
    # 兜底 markdown code block
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
    raw = raw.strip()
    try:
        layers = json.loads(raw)
    except Exception as e:
        _write_event({"type": "explain_all_parse_fail", "elapsed": elapsed,
                      "err": str(e), "raw_preview": raw[:300]})
        raise RuntimeError("step-3.6 返回非 JSON：%s" % raw[:200])

    # 兜底：如果模型返回了对象包裹（{"layers": [...]}）
    if isinstance(layers, dict):
        for k, v in layers.items():
            if isinstance(v, list):
                layers = v; break

    if not isinstance(layers, list):
        raise RuntimeError("step-3.6 返回结构不是数组：%s" % str(layers)[:200])

    _write_event({"type": "explain_all_done", "elapsed": elapsed, "n_layers": len(layers)})
    return layers


# ============================================================
# HTTP handler
# ============================================================
class Handler(http.server.SimpleHTTPRequestHandler):
    server_version = "explorecipe/0.1"

    # —— 静态资源 ——
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._serve_file(os.path.join(DOC_ROOT, "index.html"), "text/html; charset=utf-8")
            return
        # 仅放行 static/ 与 logs/images/
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):], DOC_ROOT + "/static")
            return
        if path.startswith("/logs/images/"):
            # 用户能拿到自己刚生成的图（只读访问归档目录）
            self._serve_static(path[len("/logs/images/"):], IMG_LOG_ROOT)
            return
        self.send_error(404, "Not Found")

    def _serve_file(self, abs_path, ctype):
        if not os.path.isfile(abs_path):
            self.send_error(404, "Not Found")
            return
        with open(abs_path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, rel, root):
        rel = rel.lstrip("/")
        # 防穿越
        if ".." in rel.split("/"):
            self.send_error(403)
            return
        abs_path = os.path.normpath(os.path.join(root, rel))
        if not abs_path.startswith(os.path.abspath(root)):
            self.send_error(403)
            return
        ctype = mimetypes.guess_type(abs_path)[0] or "application/octet-stream"
        self._serve_file(abs_path, ctype)

    # —— API ——
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/generate-explosion":
                self._handle_generate()
            elif path == "/api/explain-region":
                self._handle_explain()
            elif path == "/api/explain-all":
                self._handle_explain_all()
            else:
                self.send_error(404, "Not Found")
        except Exception as e:
            self._send_json(500, {"error": {"message": str(e)}})
            _write_event({"type": "api_error", "path": path, "msg": str(e)})

    def _read_body(self, max_bytes):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return b""
        if length > max_bytes:
            raise RuntimeError("Body 过大（%d > %d）" % (length, max_bytes))
        return self.rfile.read(length)

    def _send_json(self, code, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_generate(self):
        """前端 POST 原始图片字节，Content-Type 是 image/jpeg 或 image/png。"""
        body = self._read_body(MAX_UPLOAD_BYTES)
        if not body:
            raise RuntimeError("缺少上传图片")
        img_bytes = call_dmfox_explosion(body)
        b64 = base64.b64encode(img_bytes).decode("ascii")
        ext = "png" if img_bytes[:8] == b"\x89PNG\r\n\x1a\n" else "jpeg"
        self._send_json(200, {
            "ok": True,
            "image_b64": b64,
            "mime": "image/" + ext,
        })

    def _handle_explain(self):
        """前端 POST application/json: {"image_b64": "..."}"""
        body = self._read_body(MAX_REGION_BYTES)
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            raise RuntimeError("请求体不是合法 JSON")
        b64 = (payload or {}).get("image_b64") or ""
        if not b64:
            raise RuntimeError("缺少 image_b64 字段")
        try:
            region_bytes = base64.b64decode(b64)
        except Exception:
            raise RuntimeError("image_b64 解码失败")
        if len(region_bytes) > MAX_REGION_BYTES:
            raise RuntimeError("region 图片过大")
        markdown = call_stepfun_explain(region_bytes)
        self._send_json(200, {"ok": True, "markdown": markdown})

    def _handle_explain_all(self):
        """前端 POST application/json: {"image_b64": "...", "dish_hint": "..."} → 返回 layers 数组。"""
        body = self._read_body(MAX_UPLOAD_BYTES)  # explosion 整图，沿用上传上限
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            raise RuntimeError("请求体不是合法 JSON")
        b64 = (payload or {}).get("image_b64") or ""
        if not b64:
            raise RuntimeError("缺少 image_b64 字段")
        try:
            full_bytes = base64.b64decode(b64)
        except Exception:
            raise RuntimeError("image_b64 解码失败")
        layers = call_stepfun_explain_all(full_bytes, dish_hint=(payload or {}).get("dish_hint"))
        self._send_json(200, {"ok": True, "layers": layers})

    # 静默 stdout 日志（保留 stderr 错误）
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s - %s\n" % (
            datetime.datetime.now().strftime("%H:%M:%S"),
            self.address_string(), fmt % args))


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    address_family = socket.AF_INET6 if ":" in BIND_HOST else socket.AF_INET
    allow_reuse_address = True


def main():
    _ensure_log_dirs()
    print("explorecipe server listening on [%s]:%d" % (BIND_HOST, PORT))
    print("  IMAGE_API_KEY: %s" % ("set" if IMAGE_API_KEY else "MISSING"))
    print("  STEPFUN_API_KEY: %s" % ("set" if STEPFUN_API_KEY else "MISSING"))
    print("  STEPFUN_MODEL: %s" % STEPFUN_MODEL)
    httpd = ThreadingServer((BIND_HOST, PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        httpd.shutdown()


if __name__ == "__main__":
    main()

"""explorecipe Phase 2 · 本地 HTTP 服务（独立于生产 server.py）

设计依据：PHASE2_DESIGN.md
端口：默认 18083（避免与生产 18082 / 已占用 18099 冲突）

路由：
  GET  /                       → index_v2.html
  GET  /j/{id}                 → 同上，注入 window.__JOURNEY_ID__
  POST /api/start              → 上传食物图，生成 Lv1（含 MiniCPM 审核 + Gemini 识菜名 + gpt-image-2 + Gemini OCR layers）
  POST /api/drill              → {journey_id, from_level, bbox} 钻入下一层
  GET  /api/journey/{id}       → 整个 journey JSON（meta + layers 元数据全集）
  GET  /journey/{id}/layer/{n} → PNG 字节流
  GET  /api/healthz            → 健康检查

数据落盘：logs/journeys_v2/{id}/
  meta.json              所有 path
  layer_1.png            gpt-image-2 输出
  layer_1.json           Gemini 提取的 layers 元数据
  layer_2_crop.png       drill 时的裁剪图
  layer_2_brief.txt      Gemini 写的 ≤40 字简介
  layer_2.png ...        新层

核心哲学（来自 §3.5 苦涩经验）：
  - drill 时只传 crop+brief，不传整图+红框 → 避免 image-edit 模式触发的字形漂移
  - gpt-image-2 端到端创作 recipe（不让 Gemini 列大纲）
  - 标签放图片左右、不放下方
"""

# ============================================================
# stdlib
# ============================================================
import base64
import datetime
import http.server
import io
import json
import os
import re
import secrets
import socketserver
import ssl
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid


DOC_ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_ROOT = os.path.join(DOC_ROOT, "logs")
JOURNEY_ROOT = os.path.join(LOG_ROOT, "journeys_v2")
EVENT_LOG_PATH = os.path.join(LOG_ROOT, "events_v2.jsonl")
INDEX_HTML_PATH = os.path.join(DOC_ROOT, "index_v2.html")


# ============================================================
# .env 加载
# ============================================================
def _load_env():
    for fname in (".env.local", ".env"):
        p = os.path.join(DOC_ROOT, fname)
        if not os.path.exists(p):
            continue
        with open(p, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()

PORT = int(os.environ.get("PORT_V2", "18083"))
BIND_HOST = os.environ.get("BIND_HOST_V2", "::")

# —— gpt-image-2 ——
IMAGE_API_KEY = os.environ.get("IMAGE_API_KEY", "")
IMAGE_UPSTREAM_URL = os.environ.get(
    "IMAGE_UPSTREAM_URL", "https://your-openai-compatible-proxy.example/v1/images/edits"
)
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-2")
# 默认 9:16；若 PR-0 烟雾测试失败可改为 1024x1536 回退
IMAGE_SIZE_V2 = os.environ.get("IMAGE_SIZE_V2", "1024x1792")

# —— Gemini（通过 OpenAI 兼容中转，因为生产服务器无法直连 Google） ——
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")
GEMINI_BASE_URL = os.environ.get(
    "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com"
).rstrip("/")
GEMINI_TIMEOUT = int(os.environ.get("GEMINI_TIMEOUT", "180"))

# —— 内容审核 ——
# 走单次 Gemini 调用（与识菜名合并）。
# 低于此置信度的 "is_food=false" 判决放行（防止模型把边缘食物图误杀）；
# 默认 0.5：g3f 自身比 MiniCPM 稳，但极个别中转抖动时仍允许 fail-open
AUDIT_FAIL_OPEN_BELOW = float(os.environ.get("AUDIT_FAIL_OPEN_BELOW", "0.5"))

# —— 通用 ——
UPSTREAM_TIMEOUT = 600
MAX_UPLOAD_BYTES = 12 * 1024 * 1024
RETRY_TIMES = 3
RETRY_BACKOFF_BASE = 2.0
DRILL_HARD_LIMIT = 8  # current_level >= 8 拒绝继续 drill

# —— ID 字符集（沿用生产 share_id 规则）——
JOURNEY_ID_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
JOURNEY_ID_LEN = 7
_JOURNEY_ID_RE = re.compile(
    r"^[" + JOURNEY_ID_ALPHABET + r"]{" + str(JOURNEY_ID_LEN) + r"}$"
)


# ============================================================
# 工具函数
# ============================================================
def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _ensure_dirs():
    os.makedirs(LOG_ROOT, exist_ok=True)
    os.makedirs(JOURNEY_ROOT, exist_ok=True)


def _generate_journey_id():
    for _ in range(50):
        sid = "".join(
            secrets.choice(JOURNEY_ID_ALPHABET) for _ in range(JOURNEY_ID_LEN)
        )
        if not os.path.exists(os.path.join(JOURNEY_ROOT, sid)):
            return sid
    raise RuntimeError("journey_id 连续 50 次碰撞")


def _is_valid_id(sid):
    return bool(sid) and bool(_JOURNEY_ID_RE.match(sid))


def _journey_dir(sid):
    return os.path.join(JOURNEY_ROOT, sid)


def _detect_image_mime(data):
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


_log_lock = threading.Lock()

# ============================================================
# Job store · 异步任务进度（用于 LOADING 三阶段信号）
# ============================================================
JOBS = {}           # job_id -> { kind, stage, started_at, stage_started_at, done, ok, result, error }
JOBS_LOCK = threading.Lock()
JOB_TTL_SEC = 600   # 完成 10 分钟后清理
JOB_ID_LEN = 12


def _new_job_id():
    return secrets.token_hex(JOB_ID_LEN // 2 + 2)[:JOB_ID_LEN]


def _job_snapshot(job_id):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return None
        snap = dict(j)
    now = time.time()
    snap["elapsed_sec"] = round(now - snap["started_at"], 2)
    snap["stage_elapsed_sec"] = round(now - snap["stage_started_at"], 2)
    return snap


def _job_set_stage(job_id, stage):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return
        j["stage"] = stage
        j["stage_started_at"] = time.time()


def _job_finish(job_id, ok, result=None, error=None):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            return
        j["done"] = True
        j["ok"] = ok
        j["finished_at"] = time.time()
        if ok:
            j["result"] = result
            j["stage"] = "done"
        else:
            j["error"] = error
            j["stage"] = "error"


def _gc_jobs():
    """超过 TTL 的已完成 job 清掉。"""
    now = time.time()
    with JOBS_LOCK:
        for jid in list(JOBS.keys()):
            j = JOBS[jid]
            if j.get("done") and now - j.get("finished_at", now) > JOB_TTL_SEC:
                del JOBS[jid]


def _spawn_job(kind, fn, *args):
    """启动后台线程。返回 job_id。"""
    _gc_jobs()
    jid = _new_job_id()
    with JOBS_LOCK:
        JOBS[jid] = {
            "job_id": jid,
            "kind": kind,
            "stage": "queued",
            "started_at": time.time(),
            "stage_started_at": time.time(),
            "done": False,
            "ok": None,
            "result": None,
            "error": None,
        }

    def _runner():
        def progress(stage):
            _job_set_stage(jid, stage)
        try:
            result = fn(*args, on_progress=progress)
            # start_journey 返回 (jid, resp_dict)；drill 直接 dict —— 统一拿后者
            if isinstance(result, tuple) and len(result) == 2:
                result = result[1]
            _job_finish(jid, True, result=result)
        except ValueError as e:
            # 把业务级拒绝原因也打到 stderr，便于诊断（避免静默吞）
            sys.stderr.write("[job %s] ValueError: %s\n" % (jid, e))
            _job_finish(jid, False, error=str(e))
        except Exception as e:
            traceback.print_exc()
            _job_finish(jid, False, error="%s: %s" % (type(e).__name__, e))

    t = threading.Thread(target=_runner, name="job-" + jid, daemon=True)
    t.start()
    return jid


REJECTED_DIR = os.path.join(LOG_ROOT, "rejected")


def _save_rejected(img_bytes, info, reason="unknown"):
    """把审核拒绝（或低置信放行）的原图和审核响应落盘以便事后审查。"""
    try:
        os.makedirs(REJECTED_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        mime = _detect_image_mime(img_bytes)
        ext = "jpg" if mime == "image/jpeg" else (
              "png" if mime == "image/png" else (
              "webp" if mime == "image/webp" else "bin"))
        png_path = os.path.join(REJECTED_DIR, "%s_%s.%s" % (ts, reason, ext))
        json_path = os.path.join(REJECTED_DIR, "%s_%s.json" % (ts, reason))
        with open(png_path, "wb") as f:
            f.write(img_bytes)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"ts": ts, "reason": reason, "audit": info,
                       "size_bytes": len(img_bytes), "mime": mime},
                      f, ensure_ascii=False, indent=2)
        print("[rejected] saved %s (reason=%s, audit=%s)" % (
            png_path, reason, info))
    except Exception as e:
        print("[rejected] save failed: %s" % e)


def _write_event(payload):
    try:
        _ensure_dirs()
        p = dict(payload)
        p.setdefault("ts", _now_iso())
        with _log_lock:
            with open(EVENT_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")
    except Exception as e:
        print("[event] write failed: %s" % e)


def _strip_json_fence(s):
    s = (s or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
    return s.strip()


def _safe_json_loads(text):
    """容错 JSON：先 strict，失败时尝试抓第一个 {...}"""
    text = _strip_json_fence(text)
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


# ============================================================
# Prompts · gpt-image-2
# ============================================================
LV1_PROMPT_TEMPLATE = """Create a hyper-realistic VERTICAL exploded-view of "{dish_name}" shown in the input image.

The exploded view must reveal the RECIPE of this food — what ingredients go INTO it and what TOOLS are used to make it.

YOU decide the recipe. List the most accurate / interesting items:
  - 4 to 6 INGREDIENTS: discrete physical objects that go into the final dish (e.g., a slab of beef, an egg, a tomato, a piece of cheese)
  - 2 to 3 TOOLS: discrete physical objects used during cooking but not consumed (e.g., a frying pan, an oven, a chef's knife)

For each INGREDIENT row (HORIZONTAL layout per row — critical):
  - Render the object photo-realistically on ONE side (left or right) of the row
  - On the OPPOSITE side of the SAME row, render a Chinese label:
      Line 1: the Chinese name in bold (around 42pt)
      Line 2-3: an interesting ~30-character Chinese description (origin / fun fact / role — never generic praise)
  - Alternate sides between rows for visual rhythm:
      row 1: object LEFT, label RIGHT
      row 2: object RIGHT, label LEFT
      row 3: object LEFT, label RIGHT
      ...
  - DO NOT stack the label below the image; each row uses HORIZONTAL space, not vertical
  - Each row is approximately 280-360 px tall

For each TOOL row:
  - Center the tool horizontally on its own row, NO label, NO Chinese characters anywhere on or near the tool
  - Just the photorealistic object on a clean row

Strict rules (must follow):
  - All sub-items MUST be DISCRETE PHYSICAL OBJECTS, never abstract materials.
    FORBIDDEN words/concepts: 脂肪 / 蛋白质 / 糖类 / 淀粉 / 油脂 / 美拉德反应 / 发酵 / 风味 / 口感 / 调味 / 烘焙
  - All sub-items MUST be more atomic / upstream than "{dish_name}" itself.
  - Do NOT include "{dish_name}" itself as one of the sub-items.
  - Background (CRITICAL): Preserve the ORIGINAL photo's background and ambient lighting exactly — the wooden table, plate edge, surrounding props, light direction, and any out-of-focus elements behind the food. The scene should look like the original food gracefully exploded apart into its component ingredients/tools, floating in its OWN original environment. Do NOT invent a new studio backdrop, do NOT replace with a solid color, do NOT switch to a different table surface.
  - Ingredients on top, tools at the bottom. 80-120 px vertical gap between rows.

Output aspect: vertical (9:16). Rows stack vertically along the entire height.
"""


LV_NEXT_PROMPT_TEMPLATE = """The input image is a close-up crop showing: {brief}

Compose a FRESH hyper-realistic VERTICAL exploded-view that reveals the RECIPE of this object — i.e., what you would need to MAKE / PRODUCE / MANUFACTURE it.

This is a FRESH composition, NOT an edit of the input. Do not preserve any text or labels from the input. Treat the input only as a visual reference for what the target object looks like.

YOU decide the recipe. List the most accurate / interesting items:
  - 4 to 6 INGREDIENTS: discrete physical objects that go INTO the product (raw materials, parts, sub-components)
  - 2 to 3 TOOLS: discrete physical objects used during production but not consumed (machines, hand tools, implements)

For each INGREDIENT row (HORIZONTAL layout per row — critical):
  - Render the object photo-realistically on ONE side (left or right) of the row
  - On the OPPOSITE side of the SAME row, render a Chinese label:
      Line 1: the Chinese name in bold (around 42pt)
      Line 2-3: an interesting ~30-character Chinese description (origin / fun fact / role)
  - Alternate sides: row 1 object LEFT, row 2 object RIGHT, row 3 object LEFT, ...
  - DO NOT stack label below image. Use HORIZONTAL row space.

For each TOOL row:
  - Center the tool horizontally on its own row, NO label, NO Chinese characters anywhere on or near it.

Strict rules:
  - All sub-items MUST be DISCRETE PHYSICAL OBJECTS, never abstract materials.
    FORBIDDEN: 脂肪 / 蛋白质 / 糖类 / 淀粉 / 油脂 / 美拉德反应 / 发酵 / 风味 / 口感 / 调味 / 化学反应 / 物理变化
  - All sub-items MUST be more atomic / upstream than the input object.
  - Do NOT include the input object itself as one of the sub-items.
  - Background (CRITICAL): Inspect the input crop's surrounding environment — wooden table, kitchen counter, factory floor, workshop bench, soil, ocean, etc. — and PRESERVE that ambient setting in the output. The exploded scene should sit in the SAME world the input object was photographed in (same lighting direction, same surface, same atmospheric tone). Do NOT default to a studio backdrop. Do NOT replace the surface with a different material. If the crop background is ambiguous or tightly framed, infer a plausible setting from the object's natural habitat (e.g., a live goose belongs in a farm yard, not a kitchen counter) and render that consistently.
  - Ingredients on top, tools at the bottom. 80-120 px vertical gap between rows.

Output aspect: vertical (9:16). FRESH image, fresh Chinese typography — no remnants of any prior render."""


# ============================================================
# Prompts · Gemini
# ============================================================
# 入口审核 + 识菜名 · 单次调用合并版（替代旧 MiniCPM 审核 + identify_dish 两跳）
AUDIT_AND_IDENTIFY_PROMPT = """你是"美食科普"应用的入口图片识别器。一次完成两件事：

A. 审核：图片**主体**是否为可食用的食物 / 菜品 / 饮品 / 食材
B. 若是食物，给出菜名（中文 + 英文，非中国菜也用中文写）

判断规则：
- "主体"指占据画面注意力中心的物体；手、餐具、桌面、背景文字属于配角，不影响判断
- 即使是宣传图、菜单图，只要主体是食物，is_food = true
- 宠物、玩偶、毛绒玩具、人物、衣物、风景、随手物品、文档明确不是食物，is_food = false
- 模糊情况（半成品 / 原材料 / 包装食品）按"如果烹饪/打开后就是食物"判 true

严格 JSON 输出，不要额外文字：
{
  "is_food": true/false,
  "category": "food | drink | person | animal | toy | object | scene | document | other",
  "has_person": true/false,
  "confidence": 0.0-1.0,
  "reason": "≤40字中文，说明图里主体是什么",
  "dish_name_zh": "...",
  "dish_name_en": "..."
}

注意：is_food=false 时 dish_name_zh / dish_name_en 必须为空字符串 ""。
"""


EXTRACT_LAYERS_PROMPT = """这是一张爆炸分解图。它由多个垂直堆叠的"行"组成。
请提取每一行的元数据，按从上到下顺序输出。

每一行包含一个**物体**（左或右）+ 可选的**中文标签**（另一侧或在物体旁），或者只有一个**工具物体**（无标签）。

判断"行的类型"：
- 有中文标签 → kind="ingredient"（食材）
- 只有物体、无中文字符 → kind="tool"（工具）

对每一行输出：
  - kind: "ingredient" | "tool"
  - name_zh: 中文名（OCR 读图里写的字；tool 行没标签时你猜测一个常见名）
  - name_en: 英文名（OCR 读；没读到则空字符串）
  - desc_30: 30 字描述（OCR 读图里写的描述；tool 行留空）
  - bbox: [x0, y0, x1, y1]，0.0-1.0 比例，**紧紧包裹"物体本身"**（不要把中文标签框进去）

严格 JSON 输出：
{
  "layers": [
    {"kind": "ingredient", "name_zh": "...", "name_en": "...", "desc_30": "...", "bbox": [0.0, 0.0, 0.0, 0.0]},
    ...
  ]
}

要求：
- bbox 必须是 **0.0 到 1.0 之间的小数比例**（不是像素）
- 中文 OCR 如果读不清，name_zh 给 "?"（一个问号），desc_30 留空
- layers 按图中从上到下顺序排列
- 行数限制：通常 5-9 行
"""


def _brief_prompt():
    return ("用一句话（≤40 字中文）描述这张图里的主要物体。包含它的中文名。"
            "示例：'一只白色羽毛、橘色喙的活鹅，常见家禽。'"
            "只输出这一句话本身，不要 JSON、不要前后缀。")


# 抽象禁词（post-validation 标记 layer 为"探索受限"）
ABSTRACT_KEYWORDS = (
    "反应", "美拉德", "发酵", "氧化", "工艺", "现象", "过程", "变化", "原理",
    "脂肪", "蛋白质", "糖类", "淀粉", "水分", "矿物质", "维生素",
    "纤维素", "油脂", "胆固醇", "氨基酸", "碳水化合物", "微量元素", "风味",
    "口感", "调味", "烘焙", "煎制", "蒸煮", "化学", "物理变化",
)


def _is_abstract(name_zh):
    if not name_zh:
        return True
    for kw in ABSTRACT_KEYWORDS:
        if kw in name_zh:
            return True
    return False


# ============================================================
# gpt-image-2 通用调用
# ============================================================
def call_image_gen(ref_image_bytes, prompt, size=None, retries=RETRY_TIMES):
    """同步调用 gpt-image-2。返回 (img_bytes, elapsed_sec)。失败抛 RuntimeError。"""
    if not IMAGE_API_KEY:
        raise RuntimeError("IMAGE_API_KEY 未配置")
    size = size or IMAGE_SIZE_V2

    boundary = "----explorecipe-v2-" + uuid.uuid4().hex
    mime = _detect_image_mime(ref_image_bytes)
    if mime == "application/octet-stream":
        raise RuntimeError("无法识别图片格式")
    ext = mime.split("/")[-1]

    body = _build_multipart(boundary, [
        ("model", None, IMAGE_MODEL, "text/plain"),
        ("prompt", None, prompt, "text/plain"),
        ("size", None, size, "text/plain"),
        ("n", None, "1", "text/plain"),
        ("image", "ref." + ext, ref_image_bytes, mime),
    ])
    headers = {
        "Authorization": "Bearer " + IMAGE_API_KEY,
        "Content-Type": "multipart/form-data; boundary=" + boundary,
        "Content-Length": str(len(body)),
    }
    ctx = ssl.create_default_context()
    t0 = time.time()
    last_err = None

    for attempt in range(retries):
        req = urllib.request.Request(
            IMAGE_UPSTREAM_URL, data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=UPSTREAM_TIMEOUT) as resp:
                raw = resp.read()
            try:
                j = json.loads(raw.decode("utf-8"))
            except Exception:
                last_err = "上游响应非 JSON"
                continue
            if not j or "data" not in j or not j["data"]:
                last_err = "上游返回无 data: %s" % str(j)[:200]
                if attempt < retries - 1:
                    time.sleep(RETRY_BACKOFF_BASE * (attempt + 1))
                continue
            item = j["data"][0]
            if item.get("b64_json"):
                return base64.b64decode(item["b64_json"]), round(time.time() - t0, 2)
            if item.get("url"):
                with urllib.request.urlopen(item["url"], context=ctx, timeout=UPSTREAM_TIMEOUT) as ir:
                    return ir.read(), round(time.time() - t0, 2)
            last_err = "上游 data[0] 无 b64 或 url"
        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            last_err = "HTTP %d: %s" % (e.code, body_text)
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
        if attempt < retries - 1:
            time.sleep(RETRY_BACKOFF_BASE * (attempt + 1))

    raise RuntimeError("gpt-image-2 %d 次重试均失败：%s" % (retries, last_err))


# ============================================================
# Gemini 调用 · 通过 OpenAI 兼容协议（生产无法直连 Google，走中转）
# ============================================================
def _gemini_call(image_bytes, prompt, mime="image/png", expect_json=True,
                 temperature=0.3, max_tokens=12000):
    """通用 Gemini 调用（OpenAI 兼容协议）。返回 (parsed_json or text, elapsed_sec)。

    - GEMINI_BASE_URL：中转或官方 base url（含 /v1 之前的部分）
    - GEMINI_API_KEY：sk-... 形式
    - GEMINI_MODEL：gemini-3-flash-preview 等
    图片走 OpenAI chat completions 的 image_url + data:URL base64 编码。
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY 未配置")
    if mime == "application/octet-stream":
        mime = "image/png"
    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": GEMINI_MODEL,
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": "data:%s;base64,%s" % (mime, b64)}},
            ]},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = GEMINI_BASE_URL + "/v1/chat/completions"
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Authorization": "Bearer " + GEMINI_API_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    ctx = ssl.create_default_context()
    t0 = time.time()
    last_err = None
    text = None
    finish_reason = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=GEMINI_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
            j = json.loads(raw)
            choices = j.get("choices") or []
            if not choices:
                last_err = "上游无 choices: %s" % str(j)[:300]
            else:
                finish_reason = choices[0].get("finish_reason")
                msg = choices[0].get("message") or {}
                content = msg.get("content")
                # OpenAI 兼容协议下 content 是 string；少数中转返回 list 形式
                if isinstance(content, list):
                    text = "".join(
                        p.get("text", "") for p in content if isinstance(p, dict)
                    )
                else:
                    text = content or ""
                text = text.strip()
                break
        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                pass
            last_err = "HTTP %d: %s" % (e.code, body_text)
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))

    if text is None:
        raise RuntimeError("Gemini 3 次重试均失败：%s" % last_err)

    elapsed = round(time.time() - t0, 2)
    if expect_json:
        parsed = _safe_json_loads(text)
        if parsed is None:
            # finish_reason="length" 说明被 max_tokens 截断（思维模型推理吃掉预算）
            hint = "（疑似 max_tokens 截断）" if finish_reason == "length" else ""
            raise RuntimeError("Gemini 返回非 JSON%s[finish=%s]：%s"
                               % (hint, finish_reason, text[:300]))
        return parsed, elapsed
    return text, elapsed


def call_gemini_audit_and_identify(image_bytes):
    """单次 Gemini 调用，同时完成"入口审核"与"识菜名"。

    返回 dict：{
        is_food: bool, category: str, has_person: bool,
        confidence: float, reason: str,
        dish_name_zh: str, dish_name_en: str,
        elapsed: float
    }

    设计动机：取代旧 MiniCPM 审核 + identify_dish 两跳。MiniCPM 上 is_food 字段
    自相矛盾（毛绒玩具误放）、且空响应被 fail-open 漏出小猫；g3f 单调用准召率
    与 MiniCPM 持平且耗时降至 ~2.2s。详见 eval_dataset/summary.md。
    """
    mime = _detect_image_mime(image_bytes) or "image/jpeg"
    if mime == "application/octet-stream":
        mime = "image/jpeg"
    parsed, elapsed = _gemini_call(image_bytes, AUDIT_AND_IDENTIFY_PROMPT,
                                    mime=mime, expect_json=True)
    is_food = bool(parsed.get("is_food"))
    dish_zh = str(parsed.get("dish_name_zh") or "").strip()
    dish_en = str(parsed.get("dish_name_en") or "").strip()
    # 防御：模型偶有"is_food=true 但 dish_name 含'不是食物'"自相矛盾，强制改判
    if is_food and dish_zh and any(
        kw in dish_zh for kw in ("不是食物", "这是一只", "这是一个玩具", "毛绒玩具")
    ):
        is_food = False
        dish_zh = ""
        dish_en = ""
    # 反向防御：is_food=false 时 dish_name 必须为空
    if not is_food:
        dish_zh = ""
        dish_en = ""
    return {
        "is_food": is_food,
        "category": str(parsed.get("category") or "").strip().lower(),
        "has_person": bool(parsed.get("has_person")),
        "confidence": float(parsed.get("confidence") or 0.0),
        "reason": str(parsed.get("reason") or "")[:80],
        "dish_name_zh": dish_zh or ("未知食物" if is_food else ""),
        "dish_name_en": dish_en,
        "elapsed": elapsed,
    }


def call_gemini_extract_layers(image_bytes):
    parsed, elapsed = _gemini_call(image_bytes, EXTRACT_LAYERS_PROMPT,
                                    mime="image/png", expect_json=True)
    raw_layers = parsed.get("layers") or []
    out = []
    for layer in raw_layers:
        if not isinstance(layer, dict):
            continue
        bbox = layer.get("bbox") or [0, 0, 0, 0]
        # 兜底：上游偶尔给像素值
        try:
            bbox = [float(v) for v in bbox][:4]
        except Exception:
            bbox = [0.0, 0.0, 0.0, 0.0]
        if any(v > 1.5 for v in bbox):
            # 看上去是像素值，按 1024 宽 / 1792 高粗略归一化
            bbox = [bbox[0] / 1024.0, bbox[1] / 1792.0,
                    bbox[2] / 1024.0, bbox[3] / 1792.0]
        bbox = [max(0.0, min(1.0, v)) for v in bbox]
        if len(bbox) < 4:
            bbox = [0.0, 0.0, 0.0, 0.0]
        kind = str(layer.get("kind") or "ingredient").lower()
        if kind not in ("ingredient", "tool"):
            kind = "ingredient"
        name_zh = str(layer.get("name_zh") or "").strip()
        out.append({
            "kind": kind,
            "name_zh": name_zh,
            "name_en": str(layer.get("name_en") or "").strip(),
            "desc_30": str(layer.get("desc_30") or "").strip(),
            "bbox": bbox,
            "abstract": _is_abstract(name_zh),
        })
    return out, elapsed


def call_gemini_brief(crop_bytes):
    text, elapsed = _gemini_call(crop_bytes, _brief_prompt(), mime="image/png",
                                  expect_json=False, temperature=0.4)
    # 去掉模型偶发的 quotes / 前缀
    text = text.strip().strip("“”\"'`").strip()
    if len(text) > 80:
        text = text[:80].rstrip("，。") + "…"
    return {"brief": text or "未识别物体", "elapsed": elapsed}


# ============================================================
# 图像裁剪（Pillow）
# ============================================================
def crop_image_bbox(img_bytes, bbox, padding=0.04):
    """按 0-1 比例 bbox 裁剪 + 外扩 padding。返回 PNG 字节流。"""
    from PIL import Image
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = img.size
    x0, y0, x1, y1 = bbox
    # 外扩
    cx = (x0 + x1) / 2
    cy = (y0 + y1) / 2
    bw = (x1 - x0) * (1 + padding * 2)
    bh = (y1 - y0) * (1 + padding * 2)
    x0 = max(0.0, cx - bw / 2)
    y0 = max(0.0, cy - bh / 2)
    x1 = min(1.0, cx + bw / 2)
    y1 = min(1.0, cy + bh / 2)
    if x1 <= x0 or y1 <= y0:
        raise RuntimeError("bbox 无效：%s" % str(bbox))
    crop = img.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))
    # 限制 crop 最大边以减小后续 Gemini / image 上传体积
    max_side = 1024
    cw, ch = crop.size
    if max(cw, ch) > max_side:
        scale = max_side / max(cw, ch)
        crop = crop.resize((int(cw * scale), int(ch * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    crop.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ============================================================
# 分享卡 · Pillow 合成（永久链接配套）
# ============================================================
SHARE_CARD_W = 1080
SHARE_CARD_H = 1920

FONT_REG_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_BOLD_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"

PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL_V2",
    os.environ.get("PUBLIC_BASE_URL", "")
).rstrip("/")


def _share_card_path(jid):
    return os.path.join(_journey_dir(jid), "share_card.png")


def _share_url_for(jid):
    """返回分享 URL：优先 PUBLIC_BASE_URL_V2，否则用本地 host。"""
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL + "/j/" + jid
    # 本地兜底（仅本机访问能打开，QR 主要测形式）
    return "http://localhost:%d/j/%s" % (PORT, jid)


def _load_font(bold, size):
    from PIL import ImageFont
    path = FONT_BOLD_PATH if bold else FONT_REG_PATH
    # Noto CJK ttc 中 SC 子集通常在 index=2；不强求，任何 index 都能渲染中文
    try:
        return ImageFont.truetype(path, size, index=2)
    except Exception:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            return ImageFont.load_default()


def _measure(draw, text, font):
    """返回 (w, h)，兼容老版 Pillow。"""
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        try:
            return font.getsize(text)
        except Exception:
            return (len(text) * font.size // 2, font.size)


def _make_qr_image(url, box_size=10, border=2):
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M
    q = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    q.add_data(url)
    q.make(fit=True)
    return q.make_image(fill_color="black", back_color="white").convert("RGB")


def render_share_card(jid):
    """合成 9:16 分享卡。返回 PNG 字节。"""
    from PIL import Image, ImageDraw, ImageOps

    meta = read_meta(jid)
    if not meta:
        raise RuntimeError("journey 不存在")

    path_nodes = meta.get("path") or []
    if not path_nodes:
        raise RuntimeError("journey 没有任何层")

    canvas = Image.new("RGB", (SHARE_CARD_W, SHARE_CARD_H), (245, 240, 232))
    draw = ImageDraw.Draw(canvas)

    # —— 顶部 logo 条 ——
    title_band_h = 240
    draw.rectangle((0, 0, SHARE_CARD_W, title_band_h), fill=(28, 24, 22))
    f_logo = _load_font(True, 72)
    f_sub = _load_font(False, 30)
    f_dish = _load_font(True, 60)

    logo_txt = "ExploreCipe"
    lw, lh = _measure(draw, logo_txt, f_logo)
    draw.text(((SHARE_CARD_W - lw) // 2, 56), logo_txt, font=f_logo,
              fill=(255, 107, 61))

    sub_txt = "看一道菜，是怎么被造出来的"
    sw, sh = _measure(draw, sub_txt, f_sub)
    draw.text(((SHARE_CARD_W - sw) // 2, 56 + lh + 16), sub_txt, font=f_sub,
              fill=(240, 235, 228))

    # —— 菜名条 ——
    dish_txt = meta.get("dish_name_zh") or "未命名探索"
    dw, dh = _measure(draw, dish_txt, f_dish)
    draw.text(((SHARE_CARD_W - dw) // 2, title_band_h + 32),
              dish_txt, font=f_dish, fill=(28, 24, 22))

    levels_txt = "共 %d 层探索 · 钻到「%s」" % (
        len(path_nodes), (path_nodes[-1].get("title_zh") or ""))
    f_levels = _load_font(False, 28)
    lvw, lvh = _measure(draw, levels_txt, f_levels)
    draw.text(((SHARE_CARD_W - lvw) // 2,
               title_band_h + 32 + dh + 12),
              levels_txt, font=f_levels, fill=(120, 110, 100))

    # —— 中部缩略图栈 ——
    cards_top = title_band_h + 32 + dh + 12 + lvh + 36
    cards_bottom = SHARE_CARD_H - 460  # 给底部 QR 区留空间
    n = len(path_nodes)
    # 限制最多展示 5 张，>5 时取首尾 + 中间几张
    show_nodes = path_nodes
    if n > 5:
        show_nodes = [path_nodes[0], path_nodes[1], path_nodes[2],
                      path_nodes[-2], path_nodes[-1]]
    rows = len(show_nodes)
    row_h = min(180, (cards_bottom - cards_top - 12 * (rows - 1)) // rows)
    thumb_size = row_h - 12

    f_lv = _load_font(True, 30)
    f_name = _load_font(True, 36)
    f_pick = _load_font(False, 24)

    y = cards_top
    for idx, node in enumerate(show_nodes):
        # 行卡片：圆角 box（用 rectangle 模拟）
        card_x0 = 60
        card_x1 = SHARE_CARD_W - 60
        draw.rounded_rectangle((card_x0, y, card_x1, y + row_h),
                               radius=20, fill=(255, 255, 255),
                               outline=(220, 210, 200), width=2)

        # 缩略图
        layer_n = node.get("level") or (idx + 1)
        png_path = _layer_png_path(jid, layer_n)
        if os.path.exists(png_path):
            try:
                with Image.open(png_path) as im:
                    im = im.convert("RGB")
                    im = ImageOps.fit(im, (thumb_size, thumb_size),
                                       method=Image.LANCZOS)
                # 圆角粘贴
                mask = Image.new("L", (thumb_size, thumb_size), 0)
                ImageDraw.Draw(mask).rounded_rectangle(
                    (0, 0, thumb_size, thumb_size), radius=14, fill=255)
                canvas.paste(im, (card_x0 + 6, y + 6), mask)
            except Exception as e:
                print("[share-card] thumb fail lv%d: %s" % (layer_n, e))

        # 文字
        text_x = card_x0 + thumb_size + 24
        lv_txt = "Lv%d" % layer_n
        draw.text((text_x, y + 18), lv_txt, font=f_lv,
                  fill=(255, 107, 61))
        title = node.get("title_zh") or "?"
        # 截断过长
        if len(title) > 12:
            title = title[:11] + "…"
        draw.text((text_x + 80, y + 14), title, font=f_name,
                  fill=(28, 24, 22))

        # 副文：parent_picked.brief（drill 的简介）或 "入口"
        parent = node.get("parent_picked") or {}
        if node.get("parent_level") is None:
            sub = "▶ 探索入口"
        else:
            sub = "▶ " + (parent.get("brief") or parent.get("name_zh") or "")
        if len(sub) > 24:
            sub = sub[:23] + "…"
        draw.text((text_x, y + 18 + 44), sub, font=f_pick,
                  fill=(120, 110, 100))

        y += row_h + 12

    if n > 5:
        # 在中段画一个省略号节点提示
        f_omit = _load_font(False, 22)
        omit_txt = "中间 %d 层省略，扫码查看完整路径" % (n - 5)
        ow, oh = _measure(draw, omit_txt, f_omit)
        draw.text(((SHARE_CARD_W - ow) // 2, cards_top + row_h * 3),
                  omit_txt, font=f_omit, fill=(150, 140, 130))

    # —— 底部：QR + 短链 ——
    bottom_y = SHARE_CARD_H - 440
    share_url = _share_url_for(jid)
    try:
        qr_img = _make_qr_image(share_url, box_size=10, border=2)
        # 缩到合适大小
        qr_size = 280
        qr_img = qr_img.resize((qr_size, qr_size), Image.NEAREST)
        qx = 80
        qy = bottom_y + 40
        # QR 白底卡片
        draw.rounded_rectangle((qx - 16, qy - 16, qx + qr_size + 16,
                                qy + qr_size + 16),
                               radius=20, fill=(255, 255, 255),
                               outline=(220, 210, 200), width=2)
        canvas.paste(qr_img, (qx, qy))
    except Exception as e:
        print("[share-card] qr fail: %s" % e)
        qr_size = 0

    # 标语 + 链接
    f_cta = _load_font(True, 46)
    f_url = _load_font(False, 26)
    cta_x = 80 + (qr_size or 280) + 40
    draw.text((cta_x, bottom_y + 60), "扫码", font=f_cta,
              fill=(28, 24, 22))
    draw.text((cta_x, bottom_y + 60 + 60), "继续探索", font=f_cta,
              fill=(28, 24, 22))
    # URL 多行（最多 2 行）
    url_lines = [share_url[i:i + 24] for i in range(0, len(share_url), 24)][:2]
    uy = bottom_y + 60 + 60 + 72
    for line in url_lines:
        draw.text((cta_x, uy), line, font=f_url, fill=(120, 110, 100))
        uy += 32

    # —— 落盘 ——
    out_path = _share_card_path(jid)
    canvas.save(out_path, format="PNG", optimize=True)
    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def get_share_card_bytes(jid, force=False):
    """读缓存或重新合成。返回 PNG bytes。"""
    p = _share_card_path(jid)
    if not force and os.path.exists(p):
        with open(p, "rb") as f:
            return f.read()
    return render_share_card(jid)


# ============================================================
# Journey 文件 IO
# ============================================================
def _meta_path(jid):
    return os.path.join(_journey_dir(jid), "meta.json")


def _layer_png_path(jid, level):
    return os.path.join(_journey_dir(jid), "layer_%d.png" % level)


def _layer_json_path(jid, level):
    return os.path.join(_journey_dir(jid), "layer_%d.json" % level)


def _crop_png_path(jid, level):
    """level 是 drill 的 target 层（新生成层），crop 是从 level-1 来的。"""
    return os.path.join(_journey_dir(jid), "layer_%d_crop.png" % level)


def _brief_txt_path(jid, level):
    return os.path.join(_journey_dir(jid), "layer_%d_brief.txt" % level)


def read_meta(jid):
    p = _meta_path(jid)
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def write_meta(jid, meta):
    with open(_meta_path(jid), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def read_layer_meta(jid, level):
    p = _layer_json_path(jid, level)
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def write_layer_meta(jid, level, layers, elapsed=None):
    payload = {
        "level": level,
        "layers": layers,
        "extracted_at": _now_iso(),
        "extract_elapsed": elapsed,
    }
    with open(_layer_json_path(jid, level), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _pick_layer_by_bbox(layers_list, bbox):
    """返回与 bbox IoU 最大的 layer 索引 (or None)。"""
    if not layers_list:
        return None, 0.0
    bx0, by0, bx1, by1 = bbox
    barea = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    best_idx = None
    best_iou = 0.0
    for idx, layer in enumerate(layers_list):
        lb = layer.get("bbox") or [0, 0, 0, 0]
        if len(lb) < 4:
            continue
        lx0, ly0, lx1, ly1 = lb[:4]
        ix0 = max(bx0, lx0)
        iy0 = max(by0, ly0)
        ix1 = min(bx1, lx1)
        iy1 = min(by1, ly1)
        iw = max(0.0, ix1 - ix0)
        ih = max(0.0, iy1 - iy0)
        inter = iw * ih
        larea = max(0.0, lx1 - lx0) * max(0.0, ly1 - ly0)
        union = barea + larea - inter
        iou = inter / union if union > 0 else 0.0
        if iou > best_iou:
            best_iou = iou
            best_idx = idx
    return best_idx, best_iou


# ============================================================
# 业务编排 · start
# ============================================================
def start_journey(image_bytes, on_progress=None):
    """完整 Lv1 流程。返回 (jid, response_dict)。同步阻塞 ~50s。

    on_progress(stage_name)：可选回调，stage_name ∈ {'audit','image_gen','extract'}
    """
    t_total = time.time()
    cb = on_progress or (lambda *_a, **_k: None)

    # —— 1. Gemini 审核 + 识菜名（单次调用，替代旧 MiniCPM + identify 两跳） ——
    cb("audit")
    audit_info = call_gemini_audit_and_identify(image_bytes)
    if not audit_info["is_food"]:
        conf = audit_info["confidence"]
        if conf >= AUDIT_FAIL_OPEN_BELOW:
            _save_rejected(image_bytes, audit_info, reason="not_food_high_conf")
            raise ValueError(
                "REJECT_NOT_FOOD: 上传的图片似乎不是食物（置信度 %.2f，类型：%s）"
                % (conf, audit_info.get("category") or "?")
            )
        # 极低置信兜底：g3f 极个别抖动时给个机会
        print("[audit] low-conf not_food, fail-open: %s" % audit_info)
        _save_rejected(image_bytes, audit_info, reason="not_food_low_conf_passed")
        # 没有 dish_name_zh 时给一个保底
        if not audit_info.get("dish_name_zh"):
            audit_info["dish_name_zh"] = "未知食物"
    else:
        print("[audit] PASS · %s · dish=%s" % (
            audit_info.get("reason"), audit_info.get("dish_name_zh")))

    # —— 2. 创建 journey 目录 ——
    jid = _generate_journey_id()
    os.makedirs(_journey_dir(jid), exist_ok=True)

    # —— 3. gpt-image-2 生成 Lv1 ——
    cb("image_gen")
    prompt = LV1_PROMPT_TEMPLATE.format(dish_name=audit_info["dish_name_zh"])
    lv1_bytes, gen_elapsed = call_image_gen(image_bytes, prompt)
    with open(_layer_png_path(jid, 1), "wb") as f:
        f.write(lv1_bytes)

    # —— 4. Gemini 提取 Lv1 layers ——
    cb("extract")
    try:
        layers, ext_elapsed = call_gemini_extract_layers(lv1_bytes)
    except Exception as e:
        print("[start] gemini extract failed: %s" % e)
        layers, ext_elapsed = [], None
    write_layer_meta(jid, 1, layers, ext_elapsed)

    # —— 5. 写 meta.json ——
    meta = {
        "journey_id": jid,
        "created_at": _now_iso(),
        "dish_name_zh": audit_info["dish_name_zh"],
        "dish_name_en": audit_info["dish_name_en"],
        "current_level": 1,
        "path": [
            {
                "level": 1,
                "title_zh": audit_info["dish_name_zh"],
                "title_en": audit_info["dish_name_en"],
                "parent_level": None,
                "parent_picked": None,
                "image": "layer_1.png",
                "image_url": "/journey/%s/layer/1" % jid,
                "layers_meta_url": "/api/journey/%s/layer/1/meta" % jid,
                "generated_at": _now_iso(),
                "gen_elapsed_sec": gen_elapsed,
                "abstract_count": sum(1 for l in layers if l.get("abstract")),
            }
        ],
    }
    write_meta(jid, meta)

    _write_event({
        "type": "start_done", "journey_id": jid,
        "dish_zh": audit_info["dish_name_zh"],
        "gen_elapsed": gen_elapsed, "extract_elapsed": ext_elapsed,
        "total_elapsed": round(time.time() - t_total, 2),
        "audit": audit_info,
    })

    return jid, {
        "ok": True,
        "journey_id": jid,
        "current_level": 1,
        "path": meta["path"],
        "layers_meta": layers,
        "brief": audit_info["dish_name_zh"],  # Lv1 没有 brief，用菜名兜底
    }


# ============================================================
# 业务编排 · drill
# ============================================================
def drill_journey(jid, from_level, bbox, on_progress=None):
    """钻入下一层。返回 response_dict。同步阻塞 ~80s。

    on_progress(stage)：drill 没有审核阶段，stage ∈ {'identify','image_gen','extract'}
    （identify 这里实际是 'crop+brief' —— 用相同 stage 名以复用前端 UI）
    """
    t_total = time.time()
    cb = on_progress or (lambda *_a, **_k: None)
    meta = read_meta(jid)
    if not meta:
        raise ValueError("NOT_FOUND: journey 不存在")
    if from_level != meta.get("current_level"):
        raise ValueError(
            "STATE_MISMATCH: from_level=%d 但 current_level=%d"
            % (from_level, meta.get("current_level", 0))
        )
    if from_level >= DRILL_HARD_LIMIT:
        raise ValueError("DEPTH_LIMIT: 已达最深探索层（%d）" % DRILL_HARD_LIMIT)

    # —— 1. 从 layer_{from_level}.png 裁剪 bbox ——
    cb("identify")
    src_png = _layer_png_path(jid, from_level)
    with open(src_png, "rb") as f:
        src_bytes = f.read()
    crop_bytes = crop_image_bbox(src_bytes, bbox, padding=0.04)

    next_level = from_level + 1
    with open(_crop_png_path(jid, next_level), "wb") as f:
        f.write(crop_bytes)

    # —— 2. 拿父层 layer meta 选中"被框物体" ——
    parent_layers_meta = read_layer_meta(jid, from_level)
    parent_layers = (parent_layers_meta or {}).get("layers") or []
    pick_idx, iou = _pick_layer_by_bbox(parent_layers, bbox)
    target_name_zh = ""
    target_kind = "ingredient"
    if pick_idx is not None:
        pl = parent_layers[pick_idx]
        target_name_zh = pl.get("name_zh") or ""
        target_kind = pl.get("kind") or "ingredient"

    # —— 3. Gemini 看 crop 写 brief ——
    try:
        brief_info = call_gemini_brief(crop_bytes)
        brief = brief_info["brief"]
    except Exception as e:
        print("[drill] gemini brief failed: %s" % e)
        brief = target_name_zh or "未识别物体"
    with open(_brief_txt_path(jid, next_level), "w", encoding="utf-8") as f:
        f.write(brief)

    # —— 4. gpt-image-2 fresh-generate Lv N+1 ——
    cb("image_gen")
    prompt = LV_NEXT_PROMPT_TEMPLATE.format(brief=brief)
    lvn_bytes, gen_elapsed = call_image_gen(crop_bytes, prompt)
    with open(_layer_png_path(jid, next_level), "wb") as f:
        f.write(lvn_bytes)

    # —— 5. Gemini 提取 layers ——
    cb("extract")
    try:
        layers, ext_elapsed = call_gemini_extract_layers(lvn_bytes)
    except Exception as e:
        print("[drill] gemini extract failed: %s" % e)
        layers, ext_elapsed = [], None
    write_layer_meta(jid, next_level, layers, ext_elapsed)

    # —— 6. 更新 meta ——
    parent_picked = {
        "bbox": list(bbox),
        "name_zh": target_name_zh,
        "kind": target_kind,
        "iou": round(iou, 3),
        "brief": brief,
    }
    title_zh = target_name_zh or brief.split("，")[0][:8] or ("Lv%d" % next_level)
    new_node = {
        "level": next_level,
        "title_zh": title_zh,
        "title_en": "",
        "parent_level": from_level,
        "parent_picked": parent_picked,
        "image": "layer_%d.png" % next_level,
        "image_url": "/journey/%s/layer/%d" % (jid, next_level),
        "layers_meta_url": "/api/journey/%s/layer/%d/meta" % (jid, next_level),
        "generated_at": _now_iso(),
        "gen_elapsed_sec": gen_elapsed,
        "abstract_count": sum(1 for l in layers if l.get("abstract")),
        "brief": brief,
    }
    meta["path"].append(new_node)
    meta["current_level"] = next_level
    write_meta(jid, meta)

    # 失效旧的分享卡缓存（path 改变了）
    sc = _share_card_path(jid)
    if os.path.exists(sc):
        try:
            os.remove(sc)
        except Exception:
            pass

    _write_event({
        "type": "drill_done", "journey_id": jid,
        "from_level": from_level, "to_level": next_level,
        "iou": round(iou, 3), "brief": brief,
        "gen_elapsed": gen_elapsed, "extract_elapsed": ext_elapsed,
        "total_elapsed": round(time.time() - t_total, 2),
    })

    return {
        "ok": True,
        "journey_id": jid,
        "current_level": next_level,
        "path": meta["path"],
        "layers_meta": layers,
        "brief": brief,
    }


# ============================================================
# 全站 trending（opt-in publish）
# ============================================================
def _published_path(jid):
    return os.path.join(_journey_dir(jid), "published.json")


def publish_journey(jid):
    """把 journey 标记为公开（写 published.json）。返回 trending entry dict。"""
    meta = read_meta(jid)
    if not meta:
        raise ValueError("NOT_FOUND: journey 不存在")
    path_nodes = meta.get("path") or []
    if not path_nodes:
        raise ValueError("EMPTY_JOURNEY: 还没有任何层")

    lv1 = path_nodes[0]
    last = path_nodes[-1]
    entry = {
        "journey_id": jid,
        "dish_name_zh": meta.get("dish_name_zh") or "未命名探索",
        "level_count": len(path_nodes),
        "deepest_title_zh": last.get("title_zh") or "",
        "lv1_image_url": lv1.get("image_url"),
        "published_at": _now_iso(),
    }
    with open(_published_path(jid), "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False, indent=2)
    _write_event({"type": "publish", "journey_id": jid,
                  "dish_zh": entry["dish_name_zh"]})
    return entry


def unpublish_journey(jid):
    p = _published_path(jid)
    if os.path.exists(p):
        os.remove(p)
        _write_event({"type": "unpublish", "journey_id": jid})


def is_published(jid):
    return os.path.exists(_published_path(jid))


def read_trending(limit=20):
    """扫所有 published.json，按 published_at 倒序返回。"""
    if not os.path.isdir(JOURNEY_ROOT):
        return []
    entries = []
    for name in os.listdir(JOURNEY_ROOT):
        p = os.path.join(JOURNEY_ROOT, name, "published.json")
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                entries.append(json.load(f))
        except Exception:
            continue
    entries.sort(key=lambda e: e.get("published_at") or "", reverse=True)
    return entries[: max(0, int(limit))]


# ============================================================
# HTTP Handler
# ============================================================
JOURNEY_PATH_RE = re.compile(r"^/journey/([^/]+)/layer/(\d+)$")
API_LAYER_META_RE = re.compile(r"^/api/journey/([^/]+)/layer/(\d+)/meta$")
API_JOURNEY_RE = re.compile(r"^/api/journey/([^/]+)$")
API_SHARE_CARD_RE = re.compile(r"^/api/journey/([^/]+)/share-card$")
API_JOB_RE = re.compile(r"^/api/job/([0-9a-f]+)$")
API_PUBLISH_RE = re.compile(r"^/api/journey/([^/]+)/publish$")
J_PATH_RE = re.compile(r"^/j/([^/]+)$")


def _send_json(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def _send_bytes(handler, status, ctype, data, cache_seconds=0):
    handler.send_response(status)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    if cache_seconds > 0:
        handler.send_header("Cache-Control", "public, max-age=%d" % cache_seconds)
    else:
        handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def _read_index_html(injected_journey_id=None):
    if not os.path.exists(INDEX_HTML_PATH):
        return b"<h1>index_v2.html missing</h1>"
    with open(INDEX_HTML_PATH, "rb") as f:
        html = f.read()
    if injected_journey_id:
        inject = ('<script>window.__JOURNEY_ID__="%s";</script>'
                  % injected_journey_id).encode("utf-8")
        # 插在 </head> 之前；若无 </head> 就插在 <body> 之后
        if b"</head>" in html:
            html = html.replace(b"</head>", inject + b"</head>", 1)
        elif b"<body>" in html:
            html = html.replace(b"<body>", b"<body>" + inject, 1)
    return html


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "explorecipe-v2/0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (
            datetime.datetime.now().strftime("%H:%M:%S"), fmt % args
        ))

    # ---------- GET ----------
    def do_GET(self):
        try:
            self._route_get()
        except Exception as e:
            traceback.print_exc()
            _send_json(self, 500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

    def _route_get(self):
        path = self.path.split("?", 1)[0]

        if path in ("/", "/index.html"):
            html = _read_index_html()
            _send_bytes(self, 200, "text/html; charset=utf-8", html)
            return

        m = J_PATH_RE.match(path)
        if m:
            jid = m.group(1)
            inject = jid if _is_valid_id(jid) and read_meta(jid) else None
            html = _read_index_html(inject)
            _send_bytes(self, 200, "text/html; charset=utf-8", html)
            return

        if path == "/api/healthz":
            _send_json(self, 200, {
                "ok": True,
                "image_size": IMAGE_SIZE_V2,
                "audit_mode": "gemini_merged",
                "gemini_model": GEMINI_MODEL,
            })
            return

        # 阶段进度查询
        m = API_JOB_RE.match(path)
        if m:
            job_id = m.group(1)
            snap = _job_snapshot(job_id)
            if not snap:
                _send_json(self, 404, {"ok": False, "error": "job not found"})
                return
            # 不暴露线程对象等内部字段，挑要的回
            out = {
                "ok": True,
                "job_id": snap["job_id"],
                "kind": snap["kind"],
                "stage": snap["stage"],
                "elapsed_sec": snap["elapsed_sec"],
                "stage_elapsed_sec": snap["stage_elapsed_sec"],
                "done": snap["done"],
            }
            if snap["done"]:
                out["success"] = bool(snap.get("ok"))
                if snap.get("ok"):
                    out["result"] = snap.get("result")
                else:
                    out["error"] = snap.get("error")
            _send_json(self, 200, out)
            return

        # trending 列表
        if path == "/api/trending":
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            limit = 20
            for kv in qs.split("&"):
                if kv.startswith("limit="):
                    try:
                        limit = int(kv.split("=", 1)[1])
                    except Exception:
                        pass
            entries = read_trending(limit=limit)
            _send_json(self, 200, {"ok": True, "items": entries,
                                    "count": len(entries)})
            return

        m = JOURNEY_PATH_RE.match(path)
        if m:
            jid, level = m.group(1), int(m.group(2))
            if not _is_valid_id(jid):
                _send_json(self, 400, {"ok": False, "error": "invalid id"})
                return
            p = _layer_png_path(jid, level)
            if not os.path.exists(p):
                _send_json(self, 404, {"ok": False, "error": "layer not found"})
                return
            with open(p, "rb") as f:
                data = f.read()
            _send_bytes(self, 200, "image/png", data, cache_seconds=86400)
            return

        m = API_LAYER_META_RE.match(path)
        if m:
            jid, level = m.group(1), int(m.group(2))
            if not _is_valid_id(jid):
                _send_json(self, 400, {"ok": False, "error": "invalid id"})
                return
            lm = read_layer_meta(jid, level)
            if not lm:
                _send_json(self, 404, {"ok": False, "error": "not found"})
                return
            _send_json(self, 200, {"ok": True, "level": level, "layers": lm.get("layers") or []})
            return

        m = API_SHARE_CARD_RE.match(path)
        if m:
            jid = m.group(1)
            if not _is_valid_id(jid) or not read_meta(jid):
                _send_json(self, 404, {"ok": False, "error": "journey not found"})
                return
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            force = "force=1" in qs
            try:
                data = get_share_card_bytes(jid, force=force)
            except Exception as e:
                traceback.print_exc()
                _send_json(self, 500, {"ok": False,
                                       "error": "share-card 合成失败：%s" % e})
                return
            _send_bytes(self, 200, "image/png", data, cache_seconds=300)
            return

        m = API_JOURNEY_RE.match(path)
        if m:
            jid = m.group(1)
            if not _is_valid_id(jid):
                _send_json(self, 400, {"ok": False, "error": "invalid id"})
                return
            meta = read_meta(jid)
            if not meta:
                _send_json(self, 404, {"ok": False, "error": "not found"})
                return
            layers_all = {}
            for node in meta.get("path", []):
                lm = read_layer_meta(jid, node["level"])
                if lm:
                    layers_all[str(node["level"])] = lm.get("layers") or []
            _send_json(self, 200, {"ok": True, "meta": meta,
                                    "layers": layers_all,
                                    "is_published": is_published(jid)})
            return

        # 静态文件兜底（仅根目录下白名单）
        if path.startswith("/static/"):
            fpath = os.path.join(DOC_ROOT, path.lstrip("/"))
            if os.path.exists(fpath) and os.path.isfile(fpath):
                with open(fpath, "rb") as f:
                    data = f.read()
                ctype = "application/octet-stream"
                if fpath.endswith(".css"):
                    ctype = "text/css; charset=utf-8"
                elif fpath.endswith(".js"):
                    ctype = "application/javascript; charset=utf-8"
                _send_bytes(self, 200, ctype, data, cache_seconds=3600)
                return

        _send_json(self, 404, {"ok": False, "error": "not found"})

    # ---------- POST ----------
    def do_POST(self):
        try:
            self._route_post()
        except ValueError as e:
            msg = str(e)
            code = 400
            if msg.startswith("NOT_FOUND"):
                code = 404
            elif msg.startswith("STATE_MISMATCH"):
                code = 409
            elif msg.startswith("DEPTH_LIMIT"):
                code = 409
            elif msg.startswith("REJECT_NOT_FOOD"):
                code = 415
            _send_json(self, code, {"ok": False, "error": msg})
        except Exception as e:
            traceback.print_exc()
            _send_json(self, 500, {"ok": False,
                                   "error": "%s: %s" % (type(e).__name__, e)})

    def _read_body(self, max_bytes=MAX_UPLOAD_BYTES):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("BAD_REQUEST: empty body")
        if length > max_bytes:
            raise ValueError("BAD_REQUEST: body too large (%d > %d)" % (length, max_bytes))
        return self.rfile.read(length)

    def _route_post(self):
        path = self.path.split("?", 1)[0]

        if path == "/api/start":
            body = self._read_body()
            mime = _detect_image_mime(body)
            if mime == "application/octet-stream":
                raise ValueError("BAD_REQUEST: 仅支持 png/jpeg/webp/gif")
            job_id = _spawn_job("start", start_journey, body)
            _send_json(self, 202, {"ok": True, "job_id": job_id})
            return

        if path == "/api/drill":
            body = self._read_body(max_bytes=1024 * 1024)
            try:
                req = json.loads(body.decode("utf-8"))
            except Exception:
                raise ValueError("BAD_REQUEST: 非 JSON body")
            jid = str(req.get("journey_id") or "").strip()
            if not _is_valid_id(jid):
                raise ValueError("BAD_REQUEST: invalid journey_id")
            try:
                from_level = int(req.get("from_level") or 0)
            except Exception:
                raise ValueError("BAD_REQUEST: from_level 非整数")
            bbox = req.get("bbox") or []
            try:
                bbox = [float(v) for v in bbox][:4]
            except Exception:
                raise ValueError("BAD_REQUEST: bbox 非数字数组")
            if len(bbox) != 4:
                raise ValueError("BAD_REQUEST: bbox 必须 4 个数字")
            x0, y0, x1, y1 = bbox
            if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
                raise ValueError("BAD_REQUEST: bbox 必须为 0-1 且 x0<x1,y0<y1")
            job_id = _spawn_job("drill", drill_journey, jid, from_level, bbox)
            _send_json(self, 202, {"ok": True, "job_id": job_id})
            return

        m = API_PUBLISH_RE.match(path)
        if m:
            jid = m.group(1)
            if not _is_valid_id(jid):
                raise ValueError("BAD_REQUEST: invalid id")
            # 读 body（可空）：可携带 {action: "unpublish"} 取消公开
            length = int(self.headers.get("Content-Length") or 0)
            action = "publish"
            if length > 0:
                try:
                    raw = self.rfile.read(min(length, 4096))
                    body_j = json.loads(raw.decode("utf-8")) if raw else {}
                    if isinstance(body_j, dict):
                        action = str(body_j.get("action") or "publish")
                except Exception:
                    pass
            if action == "unpublish":
                unpublish_journey(jid)
                _send_json(self, 200, {"ok": True, "published": False})
                return
            entry = publish_journey(jid)
            _send_json(self, 200, {"ok": True, "published": True, "entry": entry})
            return

        _send_json(self, 404, {"ok": False, "error": "not found"})


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # address_family 在 main 里按 BIND_HOST 是否含冒号决定


def main():
    import socket as _sock
    _ensure_dirs()
    # IPv6 双栈优先；失败回退 IPv4
    family = _sock.AF_INET6 if ":" in BIND_HOST else _sock.AF_INET
    ThreadingServer.address_family = family
    srv = ThreadingServer((BIND_HOST, PORT), Handler)
    if family == _sock.AF_INET6:
        try:
            srv.socket.setsockopt(_sock.IPPROTO_IPV6, _sock.IPV6_V6ONLY, 0)
        except Exception:
            pass
    print("[explorecipe-v2] listening on http://%s:%d  (IMAGE_SIZE=%s, AUDIT=gemini_merged)"
          % (BIND_HOST, PORT, IMAGE_SIZE_V2))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[explorecipe-v2] shutdown")


if __name__ == "__main__":
    main()

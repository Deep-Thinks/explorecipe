"""
explorecipe · 本地 HTTP 服务（v1 · legacy）

设计目标：
  - 浏览器只跟 localhost 通信，密钥仅存服务端 .env
  - 两个核心后端端点：
      POST /api/generate-explosion  上传一张食物图，转发到 OpenAI 兼容反代的 gpt-image-2，
                                    返回生成的爆炸分解图 PNG
      POST /api/explain-region      上传裁切后的成分图块，转发到 StepFun step-3.6
                                    视觉模式，返回 Markdown 科普卡
  - 静态：/  或  /index.html  -> 前端 SPA

注：此为 v1 实现，当前生产已切换到 server_v2.py。保留此文件供回滚参考。
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
# 永久分享数据：每个 share_id 一个目录
SHARE_ROOT = os.path.join(LOG_ROOT, "share")
# 公开域名（用于 OG meta、二维码）；可被 .env 覆盖
PUBLIC_BASE_URL = ""  # 运行时再赋值（_load_env 之后）

# share_id 字符集：base32 去掉容易混淆的 0/O/1/I/L
SHARE_ID_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
SHARE_ID_LEN = 7  # 31^7 ≈ 2.7e10 容量，碰撞概率足够低


# ============================================================
# .env 加载（极简 K=V 行解析）
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
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:18082").rstrip("/")
IMAGE_API_KEY = os.environ.get("IMAGE_API_KEY", "")
STEPFUN_API_KEY = os.environ.get("STEPFUN_API_KEY", "")
STEPFUN_BASE_URL = os.environ.get("STEPFUN_BASE_URL", "https://api.stepfun.com/v1")
STEPFUN_MODEL = os.environ.get("STEPFUN_MODEL", "step-3.6")

UPSTREAM_TIMEOUT = 600                       # 生成图最长等 10 分钟
MAX_UPLOAD_BYTES = 12 * 1024 * 1024          # 用户上传图上限 12MB
MAX_REGION_BYTES = 4 * 1024 * 1024           # 点击成分图上限 4MB
RETRY_TIMES = 3                              # gpt-image-2 间歇性"无输出"，做指数退避重试
RETRY_BACKOFF_BASE = 2.0

IMAGE_UPSTREAM_URL = os.environ.get("IMAGE_UPSTREAM_URL",
                                    "https://your-openai-compatible-proxy.example/v1/images/edits")
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-2")
# /v1/images/edits 必须带 size；固定竖版爆炸图比例
IMAGE_SIZE = os.environ.get("IMAGE_SIZE", "1024x1536")

# —— 内容审核：MiniCPM-V 1.3B 多模态小模型 ——
# 在调用昂贵的 gpt-image-2 之前先做 is_food 二分类，拦截人物/文档/建筑等非食物图。
# 关闭：MODERATION_ENABLED=false（紧急逃生口，重启服务生效）
MINICPM_BASE_URL = os.environ.get("MINICPM_BASE_URL",
                                  "https://your-minicpm-endpoint.example/llm").rstrip("/")
MINICPM_API_KEY = os.environ.get("MINICPM_API_KEY", "")
MINICPM_MODEL = os.environ.get("MINICPM_MODEL", "MINICPM_23u6wt")  # MiniCPM-V-4.6-1.3B-Instruct
MODERATION_ENABLED = os.environ.get("MODERATION_ENABLED", "true").strip().lower() != "false"
MODERATION_MAX_SIDE = 512                    # 喂给小模型前的缩图长边像素
MODERATION_JPEG_QUALITY = 80
MODERATION_TIMEOUT = 30                      # 审核单次最长等 30s（小模型实测 ≈1-3s）
MODERATION_RETRY_TIMES = 3                   # 上游网关偶发 500/socket abort，重试再 fail-closed
MODERATION_RETRY_BACKOFF = 0.8               # 重试退避基数（短，因为审核必须低延迟）


# ============================================================
# 通用工具
# ============================================================
def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _html_attr(s):
    """转义用于 HTML 属性值的字符串。"""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;"))


def _ensure_log_dirs():
    os.makedirs(LOG_ROOT, exist_ok=True)
    os.makedirs(IMG_LOG_ROOT, exist_ok=True)
    os.makedirs(SHARE_ROOT, exist_ok=True)


# ============================================================
# share_id：永久分享单元
# ============================================================
import secrets


def _generate_share_id():
    """生成一个不与现有目录碰撞的 share_id。"""
    for _ in range(50):
        sid = "".join(secrets.choice(SHARE_ID_ALPHABET) for _ in range(SHARE_ID_LEN))
        if not os.path.exists(os.path.join(SHARE_ROOT, sid)):
            return sid
    raise RuntimeError("share_id 连续 50 次碰撞，目录可能已饱和")


_SHARE_ID_RE = re.compile(r"^[" + SHARE_ID_ALPHABET + "]{" + str(SHARE_ID_LEN) + r"}$")


def _is_valid_share_id(sid):
    return bool(sid) and bool(_SHARE_ID_RE.match(sid))


def _share_dir(sid):
    return os.path.join(SHARE_ROOT, sid)


def _persist_explosion(share_id, img_bytes, ext):
    """把生成的爆炸图 + meta 落盘到 share 目录。"""
    _ensure_log_dirs()
    d = _share_dir(share_id)
    os.makedirs(d, exist_ok=True)
    img_path = os.path.join(d, "explosion." + ext)
    with open(img_path, "wb") as f:
        f.write(img_bytes)
    meta = {
        "share_id": share_id,
        "created_at": _now_iso(),
        "image_ext": ext,
        "image_bytes": len(img_bytes),
        "explain_done": False,
    }
    _write_meta(share_id, meta)
    return img_path


def _read_meta(share_id):
    p = os.path.join(_share_dir(share_id), "meta.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_meta(share_id, meta):
    p = os.path.join(_share_dir(share_id), "meta.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def _persist_explain_to_share(share_id, explain_result):
    """explain_result = {"tagline", "dish_name", "layers"}。
    把 layers 落盘到 layers.json，tagline / dish_name 合并进 meta.json。
    """
    if not _is_valid_share_id(share_id):
        return
    d = _share_dir(share_id)
    if not os.path.isdir(d):
        # 该 share_id 尚未持久化（少见：explosion 落盘失败）
        return
    layers_path = os.path.join(d, "layers.json")
    with open(layers_path, "w", encoding="utf-8") as f:
        json.dump(explain_result.get("layers") or [], f, ensure_ascii=False, indent=2)
    meta = _read_meta(share_id) or {"share_id": share_id, "created_at": _now_iso()}
    meta["tagline"] = explain_result.get("tagline") or ""
    meta["dish_name"] = explain_result.get("dish_name") or ""
    meta["n_layers"] = len(explain_result.get("layers") or [])
    meta["explain_done"] = True
    meta["explain_done_at"] = _now_iso()
    _write_meta(share_id, meta)


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
    """按字节签名判断图片 MIME。上游反代要求 multipart 里 Content-Type 真实，否则下游 vision 拒收。"""
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

请同时输出两件事：

1. 一句**金句 tagline**（≤20 个汉字）：要让人想截图发朋友圈。聚焦图中最有趣/最反常识/最有故事的一点，
   不要平铺直述配料。不要使用感叹号堆砌。
   好例子：「这一口里藏着 4 个产地。」「沙茶酱里有 14 种香料。」「米饭比配菜更值得讲。」

2. 一个**食物名 dish_name**（≤8 个汉字）：你认为这道食物的中文菜名（如「鲜虾蛋皮饭团」「沙茶面」）。
   尽量具体，不要写「一份食物」「美味的菜」。

3. 从上到下列出图中**所有可识别的成分层**（通常 3-7 层），为每一层输出：
   - `index`: 从 0 开始的层序号（最上面是 0）
   - `name_zh`: 中文成分名（简洁，2-6 字）
   - `name_en`: 英文成分名
   - `y_ratio_top`: 该层在图中**上边缘**的相对位置（0.0=顶 ~ 1.0=底，浮点）
   - `y_ratio_bottom`: 该层**下边缘**的相对位置
   - `card`: Markdown 科普卡，结构：开头一行 `## 中文名(EN)`，然后 **一句话本质** / **起源故事** / **营养亮点** / **趣味提示** 四段，每段 30-60 字。不要编精确营养数字。

**严格输出 JSON 对象**，不要任何包裹文字、不要 markdown 代码块。例：

{
  "tagline": "这一口里藏着 4 个产地。",
  "dish_name": "鲜虾蛋皮饭团",
  "layers": [
    {"index": 0, "name_zh": "面包顶", "name_en": "Brioche Bun Top",
     "y_ratio_top": 0.04, "y_ratio_bottom": 0.22,
     "card": "## 面包顶(Brioche Bun Top)\\n\\n**一句话本质**：..."},
    ...
  ]
}
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
# 内容审核：MiniCPM-V 1.3B 多模态判 is_food
# ============================================================
MODERATION_SYSTEM_PROMPT = """你是图像内容审核器。判断图片主体内容。

严格只返回 JSON 对象（不要 markdown code block、不要任何额外文字），字段如下：
{
  "is_food": true 或 false（布尔，不要写字符串）,
  "category": 必须从以下值中选一个："food", "person", "building", "document", "scenery", "animal", "object", "other",
  "has_person": true 或 false（图中是否包含明显人脸或人物）,
  "confidence": 0.0 到 1.0 之间的浮点数,
  "reason": 不超过 30 字的中文判定理由
}

判定标准：
- 食物/菜品/饮品/食材 → is_food=true, category="food"
- 人物（即使在吃东西，只要人是主体）→ is_food=false, category="person"
- 文档/截图/网页/二维码 → is_food=false, category="document"
- 建筑/风景/动物等其他 → is_food=false, 对应 category"""


# 含人物食物图的爆炸 prompt 补丁：让 gpt-image-2 忽略画面中的人，只分解食物本身
EXPLOSION_PROMPT_IGNORE_PEOPLE = (
    "\n\nAdditional rule: The reference image may contain people "
    "(e.g., a chef, a diner, a hand holding the food). IGNORE all human "
    "figures, faces, hands, and bodies entirely — focus only on the food "
    "itself and deconstruct that. The output must not show any people."
)


def _resize_for_moderation(img_bytes, max_side=MODERATION_MAX_SIDE,
                           quality=MODERATION_JPEG_QUALITY):
    """长边缩到 max_side、转 JPEG。直接喂原图会让 MiniCPM 网关 SSL 断连。"""
    from PIL import Image  # 局部 import，未启用审核时不强制依赖
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = img.size
    scale = max_side / max(w, h)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def call_minicpm_food_check(img_bytes):
    """调 MiniCPM-V-4.6-1.3B-Instruct 做食物分类。

    返回 dict：is_food / category / has_person / confidence / reason / elapsed。
    任何失败（密钥缺、网络、解析、响应结构异常）一律抛 RuntimeError，
    由调用方按 fail-closed 策略处理。
    """
    if not MINICPM_API_KEY:
        raise RuntimeError("MINICPM_API_KEY 未配置（检查 .env）")

    small = _resize_for_moderation(img_bytes)

    b64 = base64.b64encode(small).decode("ascii")
    payload = {
        "model": MINICPM_MODEL,
        "messages": [
            {"role": "system", "content": MODERATION_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": "判断这张图。"},
                {"type": "image_url", "image_url": {
                    "url": "data:image/jpeg;base64," + b64,
                }},
            ]},
        ],
        "temperature": 0.1,
        "max_tokens": 200,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        MINICPM_BASE_URL + "/v1/chat/completions",
        data=data,
        headers={
            "Authorization": "Bearer " + MINICPM_API_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    ctx = ssl.create_default_context()
    t0 = time.time()
    last_err = None
    content = None
    for attempt in range(MODERATION_RETRY_TIMES):
        # urllib Request 重用 OK：data/headers 都是不可变快照
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=MODERATION_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
            j = json.loads(raw)
            # 网关偶发返回 {"code":500,...} 而非标准 OpenAI 结构
            if "choices" not in j:
                last_err = "上游错误响应: %s" % raw[:200]
                if attempt < MODERATION_RETRY_TIMES - 1:
                    time.sleep(MODERATION_RETRY_BACKOFF * (attempt + 1))
                continue
            content = j["choices"][0]["message"]["content"]
            break  # 成功
        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            last_err = "HTTP %d: %s" % (e.code, body_text)
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
        if attempt < MODERATION_RETRY_TIMES - 1:
            time.sleep(MODERATION_RETRY_BACKOFF * (attempt + 1))

    elapsed = round(time.time() - t0, 2)
    if content is None:
        raise RuntimeError("MiniCPM %d 次重试均失败：%s" % (MODERATION_RETRY_TIMES, last_err))

    parsed = _parse_moderation_json(content)
    if parsed is None:
        raise RuntimeError("MiniCPM 返回结构无法解析: %s" % content[:200])

    parsed["elapsed"] = elapsed
    return parsed


def _parse_moderation_json(content):
    """两阶段解析 MiniCPM 返回：先严格 json.loads，失败用正则抽核心字段。

    1.3B 小模型 JSON 输出偶发不稳（实测见过 `"reason": 中文..."` 漏左引号），
    但 `is_food` / `has_person` 这两个布尔字段格式很少出错。只要这两个能提到，
    就足以做拦截判定。

    返回 dict（含 is_food/category/has_person/confidence/reason）或 None。
    """
    if not content:
        return None
    s = content.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
    s = s.strip()

    # —— 阶段 1：严格 JSON ——
    parsed = None
    try:
        parsed = json.loads(s)
    except Exception:
        pass

    if isinstance(parsed, dict):
        is_food_raw = parsed.get("is_food")
        if isinstance(is_food_raw, str):
            is_food = is_food_raw.strip().lower() in ("true", "yes", "1")
        else:
            is_food = bool(is_food_raw)
        try:
            confidence = float(parsed.get("confidence") or 0.0)
        except Exception:
            confidence = 0.0
        return {
            "is_food": is_food,
            "category": str(parsed.get("category") or "").strip().lower(),
            "has_person": bool(parsed.get("has_person")),
            "confidence": confidence,
            "reason": str(parsed.get("reason") or "").strip()[:60],
        }

    # —— 阶段 2：正则兜底 ——
    def _extract_bool(field):
        m = re.search(
            r'"' + field + r'"\s*:\s*(true|false|"true"|"false"|"yes"|"no")',
            s, re.IGNORECASE,
        )
        if not m:
            return None
        return m.group(1).strip('"').lower() in ("true", "yes")

    def _extract_str(field):
        m = re.search(r'"' + field + r'"\s*:\s*"([^"\n]*)"', s)
        return m.group(1).strip() if m else ""

    is_food = _extract_bool("is_food")
    if is_food is None:
        return None  # 关键字段都提取不出，认输

    has_person = _extract_bool("has_person")
    return {
        "is_food": is_food,
        "category": _extract_str("category").lower(),
        "has_person": bool(has_person),
        "confidence": 0.0,
        "reason": _extract_str("reason")[:60],
    }


# ============================================================
# 上游调用：gpt-image-2 / OpenAI 兼容反代（生成爆炸分解图）
# ============================================================
def call_dmfox_explosion(ref_image_bytes, ignore_people=False):
    """同步调用上游 gpt-image-2 反代。返回 PNG 字节流。失败抛 RuntimeError。

    说明：按用户要求，不传 size 参数，让模型自选合适比例。
    """
    if not IMAGE_API_KEY:
        raise RuntimeError("IMAGE_API_KEY 未配置（检查 .env）")

    boundary = "----explorecipe-" + uuid.uuid4().hex
    ref_mime = _detect_image_mime(ref_image_bytes)
    if ref_mime == "application/octet-stream":
        raise RuntimeError("无法识别上传图片格式（仅支持 png/jpeg/webp/gif）")
    ref_ext = ref_mime.split("/")[-1]

    prompt_text = EXPLOSION_PROMPT
    if ignore_people:
        # 审核检测到图中有人 → 追加 prompt 让 gpt-image-2 忽略人物
        prompt_text = EXPLOSION_PROMPT + EXPLOSION_PROMPT_IGNORE_PEOPLE

    body = _build_multipart(boundary, [
        ("model", None, IMAGE_MODEL.encode("utf-8")),
        ("prompt", None, prompt_text.encode("utf-8")),
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
                # 等一会儿再重试，gpt-image-2 这种"无输出"经验上是间歇性
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
        raise RuntimeError("gpt-image-2 %d 次重试均失败：%s" % (RETRY_TIMES, last_err or "未知"))

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
    """新核心：一次调用让 step-3.6 看整张爆炸图。

    返回 dict: {"tagline": str, "dish_name": str, "layers": [...]}
    兼容旧版：若模型只返回数组，自动包装为 {"tagline": "", "dish_name": "", "layers": [...]}。
    """
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
        parsed = json.loads(raw)
    except Exception as e:
        _write_event({"type": "explain_all_parse_fail", "elapsed": elapsed,
                      "err": str(e), "raw_preview": raw[:300]})
        raise RuntimeError("step-3.6 返回非 JSON：%s" % raw[:200])

    # 解构出 tagline / dish_name / layers，兼容多种返回结构
    tagline = ""
    dish_name = ""
    layers = None

    if isinstance(parsed, dict):
        tagline = str(parsed.get("tagline") or "").strip()
        dish_name = str(parsed.get("dish_name") or "").strip()
        # layers 可能在 layers / data / result 等键名下
        for k in ("layers", "data", "result", "items"):
            v = parsed.get(k)
            if isinstance(v, list):
                layers = v
                break
        # 最后兜底：取第一个 list 类型的值
        if layers is None:
            for v in parsed.values():
                if isinstance(v, list):
                    layers = v
                    break
    elif isinstance(parsed, list):
        # 旧版兼容：模型只返回数组
        layers = parsed

    if not isinstance(layers, list):
        raise RuntimeError("step-3.6 返回结构无 layers 数组：%s" % str(parsed)[:200])

    _write_event({"type": "explain_all_done", "elapsed": elapsed,
                  "n_layers": len(layers), "has_tagline": bool(tagline),
                  "has_dish_name": bool(dish_name)})
    return {"tagline": tagline, "dish_name": dish_name, "layers": layers}


# ============================================================
# HTTP handler
# ============================================================
class Handler(http.server.SimpleHTTPRequestHandler):
    server_version = "explorecipe/0.1"

    # —— 静态资源 ——
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._serve_index_with_share(None)
            return
        # 永久分享页：/g/{share_id}
        if path.startswith("/g/"):
            sid = path[len("/g/"):].strip("/")
            self._serve_index_with_share(sid)
            return
        # 分享数据 API：/api/share/{id}
        if path.startswith("/api/share/"):
            sid = path[len("/api/share/"):].strip("/")
            self._handle_share_get(sid)
            return
        # 分享图（永久 URL）：/api/share/{id}/explosion
        # 用一个稳定 URL 而不是 logs/share/... 暴露磁盘结构
        if path.startswith("/share-image/"):
            sid = path[len("/share-image/"):].strip("/")
            self._handle_share_image(sid)
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

    def _serve_index_with_share(self, share_id):
        """返回 index.html。如果 share_id 合法且存在，注入 OG meta 与 window.__SHARE_ID__。"""
        index_path = os.path.join(DOC_ROOT, "index.html")
        if not os.path.isfile(index_path):
            self.send_error(404, "Not Found")
            return
        with open(index_path, "rb") as f:
            html = f.read()

        if share_id and _is_valid_share_id(share_id):
            meta = _read_meta(share_id)
            if meta:
                # 注入 OG meta（微信/小红书会抓预览）+ JS 全局 share_id
                title = meta.get("dish_name") or "ExploreCipe"
                desc = meta.get("tagline") or "解构每一口美味 · 厦门大学美食协会"
                share_url = PUBLIC_BASE_URL + "/g/" + share_id
                img_url = PUBLIC_BASE_URL + "/share-image/" + share_id

                inject_head = (
                    '<meta property="og:type" content="website">'
                    '<meta property="og:title" content="' + _html_attr(title + " · ExploreCipe") + '">'
                    '<meta property="og:description" content="' + _html_attr(desc) + '">'
                    '<meta property="og:image" content="' + _html_attr(img_url) + '">'
                    '<meta property="og:url" content="' + _html_attr(share_url) + '">'
                    '<meta name="twitter:card" content="summary_large_image">'
                ).encode("utf-8")

                inject_body = (
                    '<script>'
                    'window.__SHARE_ID__=' + json.dumps(share_id) + ';'
                    'window.__PUBLIC_BASE_URL__=' + json.dumps(PUBLIC_BASE_URL) + ';'
                    '</script>'
                ).encode("utf-8")

                # 注入到 </head> 前；body 注入到 <body> 后
                html = html.replace(b"</head>", inject_head + b"</head>", 1)
                html = html.replace(b"<body>", b"<body>" + inject_body, 1)
            # share_id 不存在：仍返回首页（前端 JS 会回退到上传流程）
        else:
            # 无 share_id：注入 PUBLIC_BASE_URL 供前端构造分享链接
            inject_body = (
                '<script>window.__PUBLIC_BASE_URL__=' +
                json.dumps(PUBLIC_BASE_URL) + ';</script>'
            ).encode("utf-8")
            html = html.replace(b"<body>", b"<body>" + inject_body, 1)

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(html)

    def _handle_share_get(self, share_id):
        """GET /api/share/{id} → 返回 {ok, dish_name, tagline, layers, image_url, created_at}"""
        if not _is_valid_share_id(share_id):
            self._send_json(404, {"error": {"message": "share_id 格式无效"}})
            return
        meta = _read_meta(share_id)
        if not meta:
            self._send_json(404, {"error": {"message": "share_id 不存在"}})
            return
        layers = []
        layers_path = os.path.join(_share_dir(share_id), "layers.json")
        if os.path.exists(layers_path):
            try:
                with open(layers_path, "r", encoding="utf-8") as f:
                    layers = json.load(f)
            except Exception:
                layers = []
        self._send_json(200, {
            "ok": True,
            "share_id": share_id,
            "dish_name": meta.get("dish_name") or "",
            "tagline": meta.get("tagline") or "",
            "layers": layers,
            "explain_done": bool(meta.get("explain_done")),
            "image_url": "/share-image/" + share_id,
            "share_url": PUBLIC_BASE_URL + "/g/" + share_id,
            "created_at": meta.get("created_at") or "",
        })

    def _handle_share_image(self, share_id):
        """GET /share-image/{id} → 返回 explosion.png/jpeg"""
        if not _is_valid_share_id(share_id):
            self.send_error(404, "Not Found")
            return
        d = _share_dir(share_id)
        for ext in ("png", "jpeg", "jpg"):
            p = os.path.join(d, "explosion." + ext)
            if os.path.isfile(p):
                ctype = "image/png" if ext == "png" else "image/jpeg"
                self._serve_file(p, ctype)
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

        # —— 内容审核（MiniCPM-V 1.3B）——
        # 在调用昂贵的 gpt-image-2 之前先做食物分类拦截。
        # 失败策略：fail-closed（审核服务挂了直接 503，不冒险烧上游 gpt-image-2 配额）。
        ignore_people = False
        if MODERATION_ENABLED:
            client_ip = self.client_address[0] if self.client_address else "-"
            try:
                mod = call_minicpm_food_check(body)
            except Exception as e:
                _write_event({
                    "type": "moderation_error",
                    "err": str(e)[:200],
                    "ip": client_ip,
                })
                self._send_json(503, {"error": {
                    "message": "图片审核服务暂时不可用，请稍后重试",
                }})
                return

            if not mod["is_food"]:
                _write_event({
                    "type": "content_blocked",
                    "category": mod["category"],
                    "has_person": mod["has_person"],
                    "confidence": mod["confidence"],
                    "reason": mod["reason"],
                    "elapsed": mod["elapsed"],
                    "ip": client_ip,
                })
                hint = mod["reason"] or mod["category"] or "非食物图片"
                self._send_json(400, {"error": {
                    "message": "请上传食物或菜品图片（识别为：%s）" % hint,
                    "category": mod["category"],
                    "code": "not_food",
                }})
                return

            ignore_people = mod["has_person"]
            _write_event({
                "type": "moderation_pass",
                "category": mod["category"],
                "has_person": mod["has_person"],
                "confidence": mod["confidence"],
                "elapsed": mod["elapsed"],
                "ip": client_ip,
            })

        img_bytes = call_dmfox_explosion(body, ignore_people=ignore_people)
        ext = "png" if img_bytes[:8] == b"\x89PNG\r\n\x1a\n" else "jpeg"

        # 生成 share_id 并落盘 explosion + 初始 meta
        share_id = _generate_share_id()
        try:
            _persist_explosion(share_id, img_bytes, ext)
        except Exception as e:
            _write_event({"type": "share_persist_fail", "share_id": share_id, "err": str(e)})
            # 持久化失败不阻塞用户：仍返回图片，但 share_id 设空
            share_id = ""

        b64 = base64.b64encode(img_bytes).decode("ascii")
        self._send_json(200, {
            "ok": True,
            "image_b64": b64,
            "mime": "image/" + ext,
            "share_id": share_id,
            "share_url": (PUBLIC_BASE_URL + "/g/" + share_id) if share_id else "",
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
        result = call_stepfun_explain_all(full_bytes, dish_hint=(payload or {}).get("dish_hint"))
        # result = {"tagline": ..., "dish_name": ..., "layers": [...]}
        share_id = (payload or {}).get("share_id") or ""
        if share_id:
            _persist_explain_to_share(share_id, result)
        resp = {"ok": True, "layers": result["layers"], "tagline": result["tagline"],
                "dish_name": result["dish_name"]}
        self._send_json(200, resp)

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

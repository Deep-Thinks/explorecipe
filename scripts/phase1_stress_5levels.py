"""Phase 1 压力测试（Recipe 模式 v2）：连续 5 层钻入

Recipe 模式核心：
- 每层产出的不是「这东西由什么组成」，而是「制作这东西的配方」=
  ingredients（融入产品的原材料，需要中文标签 + 30 字说明）
  + tools（制作时用到但不进入产品的器具，**不要中文标签**，靠视觉识别）
- 用户在 UI 上看到无标签的工具就知道"这是个可点击的菜单项"。
  圈中工具 → 钻入「这个工具是怎么造出来的」→ 食物路径自然演化成工业制造链路。

自动 picking 策略：
- Lv1：选 ingredients[0]（保持食物语境，让首次跳转仍在食物层）
- Lv2+：优先选 tools[0]（制造"啊？还能这样？"的跨域瞬间），无 tool 则选 ingredients[0]
- 跳过抽象词与父级重名

输出：
  logs/phase1/recipe_<timestamp>/
    00_input.jpg
    01_identify.json                       (含 ingredients + tools)
    level1.png … level5.png
    level1_boxed.png … level4_boxed.png
    box_id_lv*.json
    recipe_lv*.json                        (子配方：ingredients + tools)
    journey.json                           (合并路径，标注每层 picked_type)
    report.md
"""

import base64
import io
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime

from PIL import Image, ImageDraw
from google import genai
from google.genai import types


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_env(path):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env(os.path.join(ROOT, ".env"))

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")
IMAGE_API_KEY = os.environ["IMAGE_API_KEY"]
IMAGE_UPSTREAM_URL = os.environ.get(
    "IMAGE_UPSTREAM_URL", "https://image.token-recyclebin.com/v1/images/edits"
)
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gpt-image-2")
IMAGE_SIZE = "1024x1536"

TEST_IMG = os.environ.get(
    "PHASE1_INPUT", "/Token-Exchange/temp/微信图片_20260514121041_365_90.jpg"
)
OUT_DIR = os.path.join(
    ROOT, "logs", "phase1", "recipe_" + datetime.now().strftime("%Y%m%d_%H%M%S")
)
TARGET_LEVELS = 5

_gem = genai.Client(api_key=GEMINI_API_KEY)


# ============================================================
# Prompts · Gemini 端
# ============================================================
IDENTIFY_PROMPT = """识别这张食物照片。这道菜的**配方（recipe）**是什么？

请列出制作它需要的：
1. **ingredients**（原材料 / 食材 / 投入物）—— 融入最终菜品的实物
2. **tools**（工具 / 设备 / 加工器具）—— 制作时用到但不进入菜品的器具

要求严格：
- 3-5 个 ingredients，每个给 name_zh + name_en + desc_30（≤30 字中文）
- 1-3 个 tools，每个给 name_zh + name_en，desc_30 留空字符串 ""
- 必须是**离散物体**（一块鹅肝/一袋面粉/一台烤箱/一把刀），不要写"脂肪""蛋白质"这类抽象材料
- 工具要选**常见的、视觉可识别的**实物（烤箱/平底锅/刀/绞肉机/烤盘），不要写"火"这类无形概念

严格 JSON 输出：

{
  "dish_name_zh": "...",
  "dish_name_en": "...",
  "ingredients": [
    {"name_zh": "...", "name_en": "...", "desc_30": "..."},
    ...
  ],
  "tools": [
    {"name_zh": "...", "name_en": "...", "desc_30": ""},
    ...
  ]
}
"""


BOX_IDENTIFY_PROMPT = """图中有红色矩形框。识别框内是什么，严格 JSON：

{
  "name_zh": "...",
  "name_en": "...",
  "desc_30": "≤30 字中文"
}

无法辨认 → name_zh="未知"。
"""


def locate_prompt(target_zh):
    """让 Gemini 定位目标物体在图中的精确 bbox（替代几何估算）。"""
    return (
        "图中有多个垂直堆叠的物体。请定位「" + target_zh + "」这个物体的精确位置。\n\n"
        "严格 JSON 输出：\n"
        "{\n"
        '  "found": true 或 false,\n'
        '  "bbox": [x0, y0, x1, y1],\n'
        '  "confirmed_name_zh": "..."\n'
        "}\n\n"
        "**严格关键要求**：\n"
        "- bbox 四个值必须是 **0.0 到 1.0 之间的小数比例**\n"
        "- ❌ 严禁返回像素值！例如 [0, 658, 856, 793] 这种是错的！\n"
        "- ✅ 正确格式举例：\n"
        "    • 图正中央的物体 ≈ [0.2, 0.4, 0.8, 0.6]\n"
        "    • 图顶部居中的物体 ≈ [0.2, 0.05, 0.8, 0.22]\n"
        "    • 图底部居中的物体 ≈ [0.2, 0.78, 0.8, 0.95]\n"
        "    • 左上角 ≈ [0.0, 0.0, 0.3, 0.2]\n"
        "- x0/x1 = 横向比例（0=最左，1=最右）\n"
        "- y0/y1 = 纵向比例（0=最上，1=最下）\n"
        "- bbox 紧紧包裹「" + target_zh + "」**物体本身**，不要包括下方的中文标签文字\n"
        "- 如果图中没有这个物体，found=false 且 bbox=[0,0,0,0]\n"
        "- confirmed_name_zh 是你在图中实际识别到的物体名\n"
    )


def recipe_prompt(parent_zh):
    """问 Gemini：制作 parent_zh 的配方是什么。"""
    return (
        "图中红框内是「" + parent_zh + "」。请列出**制作 / 生产 / 制造**它的**配方**——\n"
        "需要的所有实体物品，分两类：\n\n"
        "【ingredients · 原材料】融入最终产品的实物（如：面粉、水、酵母、铁矿石、玻璃熔块）\n"
        "【tools · 工具】制作过程用到但不进入产品的器具（如：烤箱、平底锅、揉面机、轧钢机、电焊枪）\n\n"
        "要求严格：\n"
        "- 3-5 个 ingredients：name_zh + name_en + desc_30（≤30 字中文）\n"
        "- 1-3 个 tools：name_zh + name_en，desc_30 留空字符串 \"\"\n"
        "- 必须是**离散物体**：一袋面粉 / 一颗鸡蛋 / 一台烤箱 / 一把刀 / 一卷钢板\n"
        "- ❌ 严禁列抽象材料：脂肪、蛋白质、糖类、淀粉、油脂、纤维素、矿物质、维生素\n"
        "- ❌ 严禁列加工动作/反应：发酵、烘焙、煎制、美拉德反应、乳化、调味\n"
        "- ❌ 不要重复父级名「" + parent_zh + "」\n"
        "- ✅ 工具要常见、视觉可识别（家用 / 工业 / 实验皆可，但要能拍照）\n\n"
        "排序：按对最终产品的重要性排序，最关键的放前面。\n\n"
        "严格 JSON：\n\n"
        "{\n"
        '  "ingredients": [\n'
        '    {"name_zh": "...", "name_en": "...", "desc_30": "..."},\n'
        "    ...\n"
        "  ],\n"
        '  "tools": [\n'
        '    {"name_zh": "...", "name_en": "...", "desc_30": ""},\n'
        "    ...\n"
        "  ]\n"
        "}\n"
    )


# 抽象黑名单（pick fallback 兜底；Gemini prompt 是主防线）
ABSTRACT_KEYWORDS = (
    "反应", "工艺", "作用", "风味", "口感", "现象", "过程", "变化",
    "技术", "方法", "原理", "烹饪", "调味料",
    "脂肪", "蛋白质", "糖类", "淀粉", "水分", "矿物质", "维生素",
    "纤维素", "油脂", "胆固醇", "氨基酸", "碳水化合物", "微量元素",
)


# ============================================================
# Gemini helpers
# ============================================================
def _strip_fence(s):
    s = (s or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
    return s.strip()


def gemini_call(image_bytes, prompt, mime="image/jpeg"):
    t0 = time.time()
    resp = _gem.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime),
            prompt,
        ],
        config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            temperature=0.3,
        ),
    )
    elapsed = time.time() - t0
    text = _strip_fence(resp.text or "")
    try:
        return json.loads(text), elapsed
    except Exception as e:
        raise RuntimeError("Gemini 返回非 JSON: %s\n%s" % (e, text[:500]))


# ============================================================
# gpt-image-2
# ============================================================
def _detect_mime(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    return "image/jpeg"


def _build_multipart(boundary, fields):
    out = io.BytesIO()
    for name, filename, value, ctype in fields:
        out.write(("--" + boundary + "\r\n").encode())
        if filename:
            out.write(
                ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                 % (name, filename)).encode()
            )
            out.write(("Content-Type: %s\r\n\r\n" % ctype).encode())
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


def gpt_image_2(image_bytes, prompt, retries=3, timeout=600):
    boundary = "----explorecipe-" + uuid.uuid4().hex
    mime = _detect_mime(image_bytes)
    ext = mime.split("/")[-1]
    body = _build_multipart(boundary, [
        ("model", None, IMAGE_MODEL, "text/plain"),
        ("prompt", None, prompt, "text/plain"),
        ("size", None, IMAGE_SIZE, "text/plain"),
        ("n", None, "1", "text/plain"),
        ("image", "food." + ext, image_bytes, mime),
    ])
    headers = {
        "Authorization": "Bearer " + IMAGE_API_KEY,
        "Content-Type": "multipart/form-data; boundary=" + boundary,
        "Content-Length": str(len(body)),
    }
    ctx = ssl.create_default_context()
    last_err = None
    t0 = time.time()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                IMAGE_UPSTREAM_URL, data=body, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
                raw = resp.read()
            j = json.loads(raw.decode("utf-8"))
            if not j.get("data"):
                last_err = "no data: " + str(j)[:200]
            else:
                item = j["data"][0]
                if item.get("b64_json"):
                    return base64.b64decode(item["b64_json"]), time.time() - t0
                if item.get("url"):
                    with urllib.request.urlopen(item["url"], context=ctx, timeout=timeout) as ir:
                        return ir.read(), time.time() - t0
                last_err = "no b64 or url"
        except urllib.error.HTTPError as e:
            try:
                last_err = "HTTP %d: %s" % (e.code, e.read().decode("utf-8", "replace")[:300])
            except Exception:
                last_err = "HTTP %d" % e.code
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("gpt-image-2 %dx 失败: %s" % (retries, last_err))


# ============================================================
# Recipe 爆炸图 prompt（ingredients 有标签 / tools 无标签）
# ============================================================
def explosion_prompt(parent_zh, ingredients, tools, level=1, focus_name=None):
    item_lines = []

    # Ingredients 放上方（带 Chinese label）
    for ing in ingredients:
        item_lines.append(
            '  - [LABELED INGREDIENT] %s (%s) — Chinese description: "%s"'
            % (ing["name_zh"], ing["name_en"], ing["desc_30"])
        )
    # Tools 放下方（NO label）
    for tool in tools:
        item_lines.append(
            '  - [UNLABELED TOOL] %s (%s) — render ONLY the object photo, NO TEXT label whatsoever'
            % (tool["name_zh"], tool["name_en"])
        )
    items_block = "\n".join(item_lines)

    if level == 1:
        intro = (
            'This is a photo of "%s". Create a hyper-realistic VERTICAL exploded view '
            'showing the RECIPE — both the ingredients that go INTO this dish AND the tools '
            'used to make it.' % parent_zh
        )
        focus_rule = ""
    else:
        intro = (
            'The INPUT image has a RED rectangle highlighting "%s". '
            'Ignore everything outside the red box. Create a hyper-realistic VERTICAL exploded view '
            'showing the RECIPE of "%s" — both the ingredients used to make it AND the tools '
            'used to produce/manufacture it.' % (focus_name or parent_zh, focus_name or parent_zh)
        )
        focus_rule = (
            '\nIMPORTANT: The red rectangle MUST NOT appear in the output image. '
            'The output is the clean recipe-view of "%s" only.\n' % (focus_name or parent_zh)
        )

    return """%s

Layout (must follow exactly):
- Each listed item is its own SEPARATE LAYER stacked vertically, top to bottom.
- Render INGREDIENTS first (top), then TOOLS (bottom). 100-150px vertical gap.
- NEVER place items side-by-side.
%s
Items (top to bottom, in this exact order):
%s

CRITICAL LABELING RULES (must follow exactly):

For items marked [LABELED INGREDIENT]:
- Render the object photo-realistically
- DIRECTLY BELOW the object, render a TWO-LINE CHINESE TEXT label:
    Line 1: the Chinese name (bold, ~36pt equivalent)
    Line 2: a single line of ~30 Chinese characters with the EXACT Chinese description above
- Clean modern Chinese sans-serif (Noto Sans CJK / 思源黑体 style)
- Text color: dark gray / near-black; high legibility
- Text MUST EXACTLY match the names and descriptions (no paraphrase, no typos, no repeated characters)

For items marked [UNLABELED TOOL]:
- Render the object photo-realistically with a soft drop shadow
- ABSOLUTELY NO text label below or near this object
- ABSOLUTELY NO Chinese characters anywhere on or near this tool
- Just the clean photograph of the tool, recognizable from typical reference imagery

General rendering:
- Each item: natural colors, realistic texture, soft drop shadow on the background
- NO English captions, arrows, callouts, numbers, frames
- NO indicator lines between layers
- NO neon / magenta outlines

Background: preserve the original photo's ambient background and lighting tone (wooden table, soft light).

Strict NO-GO:
- NO label of any kind on tool items
- NO red rectangle from input shown in output
- NO solid studio background

Final image: a magazine-quality VERTICAL exploded-view column showing the RECIPE (ingredients labeled, tools unlabeled).
""" % (intro, focus_rule, items_block)


# ============================================================
# Image helpers
# ============================================================
def resize_square(raw, side=1024):
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    w, h = img.size
    s = min(w, h)
    img = img.crop(((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2))
    img = img.resize((side, side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def draw_red_box(image_bytes, bbox_ratio, width=12):
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    W, H = img.size
    x0, y0, x1, y1 = bbox_ratio
    box = (int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H))
    d = ImageDraw.Draw(img)
    for o in range(width):
        d.rectangle(
            [box[0] - o, box[1] - o, box[2] + o, box[3] + o],
            outline=(255, 0, 0),
        )
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ============================================================
# 自动 picking
# ============================================================
def _is_good(item, parent_zh):
    nm = (item.get("name_zh") or "").strip()
    if not nm:
        return False
    if parent_zh and nm == parent_zh:
        return False
    for k in ABSTRACT_KEYWORDS:
        if k in nm:
            return False
    return True


def auto_pick(ingredients, tools, level, parent_zh):
    """所有层优先 ingredients[0]（食材深拆主路径），tool 仅在无合适食材时兜底。

    通过 env PICKING_MODE 可切换：
      - "ingredient"（默认）：始终先挑食材
      - "tool"：Lv2+ 先挑工具（用于工具链彩蛋测试）
    """
    ingredients = ingredients or []
    tools = tools or []
    mode = os.environ.get("PICKING_MODE", "ingredient").strip().lower()

    if mode == "tool" and level >= 2:
        for t in tools:
            if _is_good(t, parent_zh):
                return t, "tool"

    for ing in ingredients:
        if _is_good(ing, parent_zh):
            return ing, "ingredient"

    # 食材都被过滤了 → tool 兜底
    for t in tools:
        if _is_good(t, parent_zh):
            return t, "tool"

    # 最后保底
    if ingredients:
        return ingredients[0], "ingredient"
    if tools:
        return tools[0], "tool"
    return None, None


def gemini_locate(image_bytes, target_zh):
    """让 Gemini 返回 target_zh 在图中的精确 bbox。

    返回 (bbox_tuple, confirmed_name, found, elapsed)。
    防御性处理：Gemini 偶尔返回像素值或 0-1000 范围而非 0-1 比例 → 自动归一化。
    """
    # 拿到实际图像尺寸用于归一化兜底
    try:
        with Image.open(io.BytesIO(image_bytes)) as _img:
            iw, ih = _img.size
    except Exception:
        iw, ih = 1024, 1536

    result, elapsed = gemini_call(image_bytes, locate_prompt(target_zh), mime="image/png")
    bbox_raw = result.get("bbox") or [0, 0, 0, 0]
    try:
        vals = [float(v) for v in bbox_raw[:4]]
    except Exception:
        vals = [0.0, 0.0, 0.0, 0.0]

    # 归一化：检测返回值的尺度
    max_v = max(abs(v) for v in vals) if vals else 0.0
    if max_v > 1.5:
        # 不是 0-1 范围。尝试两种 fallback：
        if max_v <= 1000.1:
            # Gemini-spatial 经典约定：0-1000 范围
            x0, y0, x1, y1 = vals
            # 但 Gemini 也可能直接返回像素值。判断：如果最大值接近 1000 → 视为 0-1000；
            # 如果接近 image 维度 → 视为像素
            if max_v > max(iw, ih) * 0.5:
                # 应是像素值
                vals = [x0 / iw, y0 / ih, x1 / iw, y1 / ih]
            else:
                vals = [v / 1000.0 for v in vals]
        else:
            # 显然像素值
            x0, y0, x1, y1 = vals
            vals = [x0 / iw, y0 / ih, x1 / iw, y1 / ih]

    # 安全：clamp + 保证 y0<y1, x0<x1
    vals = [max(0.0, min(1.0, v)) for v in vals]
    x0, y0, x1, y1 = vals
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    bbox = (x0, y0, x1, y1)

    found = bool(result.get("found")) and (x1 - x0) > 0.01 and (y1 - y0) > 0.01
    confirmed = (result.get("confirmed_name_zh") or "").strip() or target_zh
    return bbox, confirmed, found, elapsed


def _fallback_bbox(slot, total):
    """Gemini locate 失败时的兜底：几何估算。"""
    if total <= 0:
        return (0.15, 0.04, 0.85, 0.22)
    slot_h = 1.0 / total
    pad = slot_h * 0.10
    y0 = slot * slot_h + pad
    y1 = (slot + 1) * slot_h - pad
    return (0.18, max(0.02, y0), 0.82, min(0.98, y1))


def _expand_bbox(bbox, pad_x=0.02, pad_y=0.015):
    """把 bbox 稍微扩边 padding，避免红框太贴边遮住物体。"""
    x0, y0, x1, y1 = bbox
    return (
        max(0.0, x0 - pad_x),
        max(0.0, y0 - pad_y),
        min(1.0, x1 + pad_x),
        min(1.0, y1 + pad_y),
    )


# ============================================================
# 主流程
# ============================================================
def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print("Recipe 模式压力测试输出: %s\n" % OUT_DIR)

    # Step 0
    print("[0] 读图")
    raw = open(TEST_IMG, "rb").read()
    input_bytes = resize_square(raw)
    with open(os.path.join(OUT_DIR, "00_input.jpg"), "wb") as f:
        f.write(input_bytes)

    # Step 1: identify dish + tools
    print("[1] Gemini 识别整道菜（含 ingredients + tools）")
    identify, t_id = gemini_call(input_bytes, IDENTIFY_PROMPT, mime="image/jpeg")
    n_ing = len(identify.get("ingredients") or [])
    n_tool = len(identify.get("tools") or [])
    print("    [%5.2fs] %s — %d ingredients + %d tools" %
          (t_id, identify["dish_name_zh"], n_ing, n_tool))
    for ing in identify.get("ingredients", []):
        print("      INGREDIENT  %s — %s" % (ing["name_zh"], ing["desc_30"]))
    for tool in identify.get("tools", []):
        print("      TOOL        %s" % tool["name_zh"])
    with open(os.path.join(OUT_DIR, "01_identify.json"), "w", encoding="utf-8") as f:
        json.dump(identify, f, ensure_ascii=False, indent=2)

    # Level 1
    print("[2] gpt-image-2 第 1 层")
    p1 = explosion_prompt(
        identify["dish_name_zh"],
        identify["ingredients"],
        identify["tools"],
        level=1,
    )
    level_img, t_lv = gpt_image_2(input_bytes, p1)
    print("    [%5.2fs] %dKB" % (t_lv, len(level_img) // 1024))
    with open(os.path.join(OUT_DIR, "level1.png"), "wb") as f:
        f.write(level_img)

    journey = [{
        "level": 1,
        "parent": "(根)",
        "title": identify["dish_name_zh"],
        "title_en": identify["dish_name_en"],
        "ingredients": identify["ingredients"],
        "tools": identify["tools"],
        "image": "level1.png",
        "boxed_input": None,
        "gen_elapsed": round(t_lv, 2),
        "gemini_id_elapsed": round(t_id, 2),
    }]
    timings = [("识别整道菜配方 [Gemini]", t_id),
               ("Lv1 [gpt-image-2]", t_lv)]

    current_image = level_img
    current_ingredients = identify["ingredients"]
    current_tools = identify["tools"]
    current_title = identify["dish_name_zh"]

    # Drill levels 2..N
    for lv in range(2, TARGET_LEVELS + 1):
        print("\n== Drill to Level %d ==" % lv)
        picked, ptype = auto_pick(
            current_ingredients, current_tools, lv, current_title
        )
        if not picked:
            print("  无可挑项，停止")
            break
        print("  挑选 [%s]: %s (%s)" %
              (ptype.upper(), picked["name_zh"], picked["name_en"]))

        # 用 Gemini 拿到 picked 物体的精确 bbox（替代几何估算）
        try:
            bbox, confirmed_name, found, t_loc = gemini_locate(current_image, picked["name_zh"])
        except Exception as e:
            print("  Gemini 定位失败: %s" % e)
            break
        if not found or bbox == (0, 0, 0, 0):
            print("  [%5.2fs] Gemini 没找到 %s，启用 fallback bbox" % (t_loc, picked["name_zh"]))
            total = len(current_ingredients) + len(current_tools)
            n_ing = len(current_ingredients)
            if ptype == "tool":
                idx_in_list = next(
                    (i for i, t in enumerate(current_tools) if t["name_zh"] == picked["name_zh"]),
                    0,
                )
                slot = n_ing + idx_in_list
            else:
                slot = next(
                    (i for i, x in enumerate(current_ingredients) if x["name_zh"] == picked["name_zh"]),
                    0,
                )
            bbox = _fallback_bbox(slot, total)
        else:
            print("  [%5.2fs] Gemini 定位: bbox=%s, 确认名=%s" %
                  (t_loc, tuple(round(v, 3) for v in bbox), confirmed_name))

        # 红框稍微扩边
        bbox_drawn = _expand_bbox(bbox)
        boxed = draw_red_box(current_image, bbox_drawn, width=12)
        boxed_name = "level%d_boxed.png" % (lv - 1)
        with open(os.path.join(OUT_DIR, boxed_name), "wb") as f:
            f.write(boxed)

        # 用 confirmed_name 作为这一层标题
        title_name = confirmed_name or picked["name_zh"]
        with open(os.path.join(OUT_DIR, "box_id_lv%d.json" % (lv - 1)), "w", encoding="utf-8") as f:
            json.dump({
                "expected": picked, "expected_type": ptype,
                "bbox": list(bbox), "found": found,
                "confirmed_name": title_name,
            }, f, ensure_ascii=False, indent=2)

        try:
            recipe, t_rc = gemini_call(boxed, recipe_prompt(title_name), mime="image/png")
        except Exception as e:
            print("  Gemini 列子配方失败: %s" % e)
            break
        sub_ing = recipe.get("ingredients") or []
        sub_tool = recipe.get("tools") or []
        print("  [%5.2fs] 配方: %d ingredients + %d tools" %
              (t_rc, len(sub_ing), len(sub_tool)))
        for ing in sub_ing:
            print("      INGREDIENT  %s — %s" % (ing["name_zh"], ing.get("desc_30", "")))
        for tool in sub_tool:
            print("      TOOL        %s" % tool["name_zh"])
        with open(os.path.join(OUT_DIR, "recipe_lv%d.json" % (lv - 1)), "w", encoding="utf-8") as f:
            json.dump(recipe, f, ensure_ascii=False, indent=2)

        try:
            pN = explosion_prompt(
                title_name, sub_ing, sub_tool,
                level=2, focus_name=title_name,
            )
            level_img, t_lv = gpt_image_2(boxed, pN)
        except Exception as e:
            print("  gpt-image-2 第 %d 层失败: %s" % (lv, e))
            break
        print("  [%5.2fs] %dKB" % (t_lv, len(level_img) // 1024))
        lv_name = "level%d.png" % lv
        with open(os.path.join(OUT_DIR, lv_name), "wb") as f:
            f.write(level_img)

        journey.append({
            "level": lv,
            "parent": current_title,
            "chosen_from_prev": picked["name_zh"],
            "chosen_type": ptype,
            "title": title_name,
            "title_en": picked.get("name_en", ""),
            "bbox": list(bbox),
            "bbox_found": found,
            "ingredients": sub_ing,
            "tools": sub_tool,
            "image": lv_name,
            "boxed_input": boxed_name,
            "gen_elapsed": round(t_lv, 2),
            "gemini_locate_elapsed": round(t_loc, 2),
            "gemini_recipe_elapsed": round(t_rc, 2),
        })
        timings.append(("Lv%d 定位红框 [Gemini]" % lv, t_loc))
        timings.append(("Lv%d 配方 [Gemini]" % lv, t_rc))
        timings.append(("Lv%d [gpt-image-2]" % lv, t_lv))

        current_image = level_img
        current_ingredients = sub_ing
        current_tools = sub_tool
        current_title = title_name

    # Save journey
    with open(os.path.join(OUT_DIR, "journey.json"), "w", encoding="utf-8") as f:
        json.dump(journey, f, ensure_ascii=False, indent=2)

    # Report
    total = sum(t for _, t in timings)
    lines = ["# Phase 1 · Recipe 模式 5 层压力测试报告\n",
             "- 时间: %s" % datetime.now().isoformat(timespec="seconds"),
             "- 输出: %s" % OUT_DIR,
             "- 完成层数: %d / %d\n" % (len(journey), TARGET_LEVELS),
             "## 探索路径\n"]
    for j in journey:
        if j["level"] == 1:
            lines.append("- **Lv1**: %s (%s) — %d ing + %d tool" %
                         (j["title"], j["title_en"],
                          len(j["ingredients"]), len(j["tools"])))
        else:
            lines.append("  ↓ 钻入 [%s] (%s)" %
                         (j["chosen_from_prev"], j["chosen_type"].upper()))
            lines.append("- **Lv%d**: %s (%s) — %d ing + %d tool" %
                         (j["level"], j["title"], j.get("title_en", ""),
                          len(j["ingredients"]), len(j["tools"])))
    lines.append("\n## 耗时")
    for name, t in timings:
        lines.append("- %-40s %6.2fs" % (name, t))
    lines.append("- **合计**: %.2fs" % total)
    report = "\n".join(lines)
    with open(os.path.join(OUT_DIR, "report.md"), "w", encoding="utf-8") as f:
        f.write(report)
    print("\n" + "=" * 60)
    print(report)


if __name__ == "__main__":
    main()

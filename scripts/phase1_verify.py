"""Phase 1 端到端验证：「无限分解」MVP 链路

流程：
  原图(jpg)
   → Gemini 3 Flash 识别整道菜（菜名 + 4-6 成分 + 30字说明）
   → gpt-image-2 第 1 层爆炸图（含中文标签 + 30字说明，all-in-one）
   → PIL 在第 1 层图顶部画红框（模拟用户框选 components[0]）
   → Gemini 3 Flash 识别红框内容
   → Gemini 3 Flash 列子部件
   → gpt-image-2 第 2 层爆炸图（仅分解红框内部件）

输出目录：logs/phase1/<timestamp>/
  00_input.jpg / 01_identify.json / 02_level1.png /
  03_level1_boxed.png / 04_box_identify.json / 05_sub_components.json /
  06_level2.png / report.md

依赖：google-genai 2.x（pip install google-genai --break-system-packages --ignore-installed），Pillow
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


# ============================================================
# 环境
# ============================================================
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
    ROOT, "logs", "phase1", datetime.now().strftime("%Y%m%d_%H%M%S")
)


# ============================================================
# Gemini 调用（thinking_budget=0，强制不推理）
# ============================================================
_gem_client = genai.Client(api_key=GEMINI_API_KEY)


IDENTIFY_PROMPT = """识别这张食物照片。严格返回 JSON 对象（不要 markdown code block，不要任何包裹文字）：

{
  "dish_name_zh": "...",
  "dish_name_en": "...",
  "components": [
    {"name_zh": "面饼", "name_en": "Pizza Dough", "desc_30": "意大利经典发酵面饼，外脆内软。"},
    ...
  ]
}

要求：
- dish_name_zh 中文菜名 2-8 字，尽量具体
- components 列出 4-6 个主要可见成分，按视觉显著程度从强到弱
- desc_30 是 ≤30 个汉字的有趣科普（产地/营养/趣闻任选一角度），不要平铺直述
"""


BOX_IDENTIFY_PROMPT = """图中有一个红色矩形框。请识别框内的食物部件，严格返回 JSON：

{
  "name_zh": "...",
  "name_en": "...",
  "desc_30": "≤30 字中文科普"
}

如果红框内难以辨认，name_zh 写「未知」。
"""


def _strip_fence(s):
    s = (s or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
    return s.strip()


def gemini_call(image_bytes, prompt, mime="image/jpeg"):
    """单次 Gemini 视觉调用。thinking_budget=0。返回 (dict, elapsed)。"""
    t0 = time.time()
    resp = _gem_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime),
            prompt,
        ],
        config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            temperature=0.2,
        ),
    )
    elapsed = time.time() - t0
    text = _strip_fence(resp.text or "")
    try:
        return json.loads(text), elapsed
    except Exception as e:
        raise RuntimeError(f"Gemini 返回非 JSON：{e}\n--- raw ---\n{text[:500]}")


# ============================================================
# gpt-image-2 调用（沿用 server.py 的 multipart 思路）
# ============================================================
def _detect_mime(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
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
    """同步调用 token-recyclebin gpt-image-2。返回 (image_bytes, elapsed)。"""
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
    raise RuntimeError("gpt-image-2 %dx 失败：%s" % (retries, last_err))


# ============================================================
# 爆炸图 prompt：all-in-one（图含中文标签 + 30字说明）
# ============================================================
def explosion_prompt(dish_zh, components, level=1, focus_name=None):
    parts_lines = []
    for c in components:
        parts_lines.append(
            '    • %s (%s) — 描述: %s' % (c["name_zh"], c["name_en"], c["desc_30"])
        )
    parts_block = "\n".join(parts_lines)

    if level == 1:
        intro = (
            'This is a photo of "%s". Create a hyper-realistic VERTICAL exploded view, '
            'with each component as a separate floating layer.' % dish_zh
        )
        focus_rule = ""
    else:
        intro = (
            'The INPUT image has a RED rectangle highlighting "%s". '
            'Ignore everything outside the red box. Create a hyper-realistic VERTICAL exploded view '
            'that decomposes ONLY the contents inside the red box, showing the sub-ingredients '
            'that make up that item.' % (focus_name or dish_zh)
        )
        focus_rule = (
            '\nIMPORTANT: The red rectangle MUST NOT appear in the output image. '
            'The output is the clean exploded view of "%s" only.\n' % (focus_name or dish_zh)
        )

    return """%s

Layout (must follow exactly):
- Each listed component is its own SEPARATE LAYER stacked vertically (top to bottom).
- Each layer centered horizontally. 100-150px vertical gap between layers.
- NEVER place components side-by-side.
%s
Components (top to bottom, with REQUIRED Chinese labels):
%s

Each layer rendering:
- Photo-realistic ingredient with natural colors, texture, and a soft drop shadow.
- DIRECTLY BELOW each ingredient, render a TWO-LINE CHINESE TEXT label:
    Line 1: the Chinese name (bold, ~36pt equivalent, e.g., 面饼).
    Line 2: a single line of ~30 Chinese characters with the exact description text given above.
- Use a clean modern Chinese sans-serif (Noto Sans CJK / 思源黑体 style).
- Text color: dark gray / near-black; high legibility against the soft background.
- DO NOT add English captions, arrows, callouts, numbers, frames, or decorations.
- The Chinese text MUST EXACTLY match the names and descriptions given above (no paraphrase, no typos).

Background:
Preserve the original photo's ambient background and lighting tone.

Strict NO-GO:
- NO indicator lines, arrows, or numbering between layers.
- NO neon/magenta outlines on layers. (Any red rectangle from the input must NOT appear.)
- NO solid studio background.

Final image: a magazine-quality VERTICAL exploded-view column with Chinese labels under each layer.
""" % (intro, focus_rule, parts_block)


# ============================================================
# 图像辅助
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
    """bbox_ratio: (x0,y0,x1,y1) in [0,1]. 返回 PNG bytes。"""
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
# 主流程
# ============================================================
def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print("Phase 1 输出目录: %s\n" % OUT_DIR)

    # —— Step 0: 读图 + 预处理 ——
    print("[0] 读取测试图：%s" % TEST_IMG)
    if not os.path.exists(TEST_IMG):
        print("ERROR: 测试图不存在，请设置 PHASE1_INPUT 环境变量")
        sys.exit(1)
    raw = open(TEST_IMG, "rb").read()
    print("    原图 %dKB → 居中裁正方 → 1024×1024 jpeg" % (len(raw) // 1024))
    input_bytes = resize_square(raw)
    with open(os.path.join(OUT_DIR, "00_input.jpg"), "wb") as f:
        f.write(input_bytes)

    # —— Step 1: Gemini 识别整道菜 ——
    print("\n[1] Gemini 识别整道菜（thinking_budget=0）")
    identify, t1 = gemini_call(input_bytes, IDENTIFY_PROMPT, mime="image/jpeg")
    print("    耗时 %.2fs" % t1)
    print("    菜名: %s (%s)" % (identify["dish_name_zh"], identify["dish_name_en"]))
    print("    成分:")
    for c in identify["components"]:
        print("      - %s (%s) — %s" % (c["name_zh"], c["name_en"], c["desc_30"]))
    with open(os.path.join(OUT_DIR, "01_identify.json"), "w", encoding="utf-8") as f:
        json.dump(identify, f, ensure_ascii=False, indent=2)

    # —— Step 2: gpt-image-2 第 1 层 ——
    print("\n[2] gpt-image-2 生成第 1 层爆炸图（含中文标签 + 30 字说明）")
    p1 = explosion_prompt(identify["dish_name_zh"], identify["components"], level=1)
    level1, t2 = gpt_image_2(input_bytes, p1)
    print("    耗时 %.2fs，输出 %dKB" % (t2, len(level1) // 1024))
    with open(os.path.join(OUT_DIR, "02_level1.png"), "wb") as f:
        f.write(level1)

    # —— Step 3: 在第 1 层顶部画红框（选 components[0]）——
    chosen = identify["components"][0]
    print("\n[3] 画红框（模拟用户框选 components[0] = %s）" % chosen["name_zh"])
    # 1024×1536 竖图。第一个成分通常在顶部约 4%-22% 高度，居中。
    bbox = (0.18, 0.04, 0.82, 0.22)
    boxed = draw_red_box(level1, bbox, width=12)
    with open(os.path.join(OUT_DIR, "03_level1_boxed.png"), "wb") as f:
        f.write(boxed)
    print("    bbox(比例) = %s, 红框粗 12px" % str(bbox))

    # —— Step 4: Gemini 识别红框 ——
    print("\n[4] Gemini 识别红框内是什么")
    box_id, t4 = gemini_call(boxed, BOX_IDENTIFY_PROMPT, mime="image/png")
    print("    耗时 %.2fs" % t4)
    print("    识别: %s (%s) — %s" %
          (box_id["name_zh"], box_id.get("name_en", "?"), box_id.get("desc_30", "")))
    match = box_id["name_zh"] == chosen["name_zh"]
    print("    与预期 [%s] 一致: %s" % (chosen["name_zh"], "✓" if match else "✗"))
    with open(os.path.join(OUT_DIR, "04_box_identify.json"), "w", encoding="utf-8") as f:
        json.dump({"expected": chosen, "got": box_id, "match": match},
                  f, ensure_ascii=False, indent=2)

    # —— Step 5: Gemini 列出红框目标的子部件 ——
    print("\n[5] Gemini 列出 [%s] 的 3-5 个子部件" % box_id["name_zh"])
    sub_prompt = (
        "图中红框内是「%s」。请列出它的 3-5 个主要组成成分或制作要素，"
        "按重要性排序。严格返回 JSON：\n\n"
        "{\n"
        '  "components": [\n'
        '    {"name_zh": "...", "name_en": "...", "desc_30": "..."},\n'
        "    ...\n"
        "  ]\n"
        "}\n\n"
        "desc_30 是 ≤30 字的中文科普。"
    ) % box_id["name_zh"]
    sub, t5 = gemini_call(boxed, sub_prompt, mime="image/png")
    print("    耗时 %.2fs" % t5)
    print("    子部件:")
    for c in sub["components"]:
        print("      - %s (%s) — %s" % (c["name_zh"], c["name_en"], c["desc_30"]))
    with open(os.path.join(OUT_DIR, "05_sub_components.json"), "w", encoding="utf-8") as f:
        json.dump(sub, f, ensure_ascii=False, indent=2)

    # —— Step 6: gpt-image-2 第 2 层 ——
    print("\n[6] gpt-image-2 生成第 2 层爆炸图（仅分解红框内 [%s]）" % box_id["name_zh"])
    p2 = explosion_prompt(
        box_id["name_zh"], sub["components"], level=2, focus_name=box_id["name_zh"]
    )
    level2, t6 = gpt_image_2(boxed, p2)
    print("    耗时 %.2fs，输出 %dKB" % (t6, len(level2) // 1024))
    with open(os.path.join(OUT_DIR, "06_level2.png"), "wb") as f:
        f.write(level2)

    # —— 报告 ——
    total = t1 + t2 + t4 + t5 + t6
    report = """# Phase 1 验证报告

- 时间: %s
- 输出目录: %s
- 测试图: %s

## 耗时
| 步骤 | 调用 | 耗时 (s) |
|---|---|---|
| 1 | Gemini 识别整道菜 | %.2f |
| 2 | gpt-image-2 第 1 层 | %.2f |
| 4 | Gemini 识别红框 | %.2f |
| 5 | Gemini 列子部件 | %.2f |
| 6 | gpt-image-2 第 2 层 | %.2f |
| — | **合计** | **%.2f** |

## 识别结果
- 菜名: **%s** (%s)
- 第 1 层成分: %s
- 红框预期: %s
- 红框实测: %s  (一致: %s)
- 子部件: %s

## 待人工评估
1. **02_level1.png** 中文标签是否清晰、无错字？是否真的把 30 字说明渲染到了图里？
2. **02_level1.png** 是否符合"上下垂直爆炸 + 每层下方标签+描述"的版面？
3. **03→04** 红框识别是否准确（应等于 components[0]）？
4. **06_level2.png** 是否真的分解了红框内的 [%s]（而不是把整道菜又分解了一遍）？
5. **06_level2.png** 输出图中是否成功移除了输入的红框（重要：否则第 3 层会嵌套红框）？
""" % (
        datetime.now().isoformat(timespec="seconds"),
        OUT_DIR,
        TEST_IMG,
        t1, t2, t4, t5, t6, total,
        identify["dish_name_zh"], identify["dish_name_en"],
        ", ".join(c["name_zh"] for c in identify["components"]),
        chosen["name_zh"],
        box_id["name_zh"], "✓" if match else "✗",
        ", ".join(c["name_zh"] for c in sub["components"]),
        box_id["name_zh"],
    )
    with open(os.path.join(OUT_DIR, "report.md"), "w", encoding="utf-8") as f:
        f.write(report)
    print("\n" + "=" * 60)
    print(report)


if __name__ == "__main__":
    main()

"""压力测试：4 道不同结构的食物，全链路跑通 + step-3.6 批量识别评分。

流水线：
  text-to-image 生成源食物照 → POST /api/generate-explosion 拿爆炸图 → step-3.6 批量解读
"""
import base64
import io
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

from openai import OpenAI

OUT_DIR = "/Token-Exchange/temp/temp2/stress"
SERVER = "http://localhost:18081"

# ---- API keys ----
env = {l.split("=", 1)[0].strip(): l.split("=", 1)[1].strip()
       for l in open("/niuniu869_dev/explorecipe/.env")
       if "=" in l and not l.startswith("#")}
IMAGE_KEY = env["IMAGE_API_KEY"]
IMAGE_URL = env.get("IMAGE_UPSTREAM_URL", "https://image.token-recyclebin.com/v1/images/edits")
STEPFUN_KEY = env["STEPFUN_API_KEY"]

# ---- 测试食物 ----
FOODS = [
    {"id": "ramen", "name_zh": "豚骨拉面",
     "gen_prompt": "A photograph of a Japanese tonkotsu ramen bowl, top-down view, white ramen broth, "
                   "wavy yellow noodles visible, two slices of chashu pork, half a soft-boiled egg with "
                   "golden yolk, chopped green scallions, sheet of nori seaweed, wooden table. "
                   "Studio lighting, hyper-realistic, 4K.",
     "expected": ["拉面", "面", "叉烧", "鸡蛋", "葱", "海苔", "汤", "noodle", "egg", "broth", "pork", "scallion"]},
    {"id": "pizza", "name_zh": "玛格丽特披萨",
     "gen_prompt": "A photograph of a Margherita pizza slice on a white plate, side-up view, "
                   "thin crispy crust, tomato sauce, melted mozzarella cheese, fresh basil leaves on top, "
                   "drizzle of olive oil. Studio lighting, hyper-realistic, 4K.",
     "expected": ["饼", "皮", "番茄", "酱", "奶酪", "芝士", "罗勒", "橄榄油", "crust", "sauce", "cheese", "basil"]},
    {"id": "sushi", "name_zh": "三文鱼寿司",
     "gen_prompt": "A photograph of three salmon nigiri sushi pieces on a wooden board, side view, "
                   "white rice base with orange salmon slice on top, hint of wasabi, dab of soy sauce. "
                   "Studio lighting, hyper-realistic, 4K.",
     "expected": ["米饭", "饭", "三文鱼", "鱼", "海苔", "芥末", "酱油", "rice", "salmon", "wasabi", "nori"]},
    {"id": "bubbletea", "name_zh": "珍珠奶茶",
     "gen_prompt": "A photograph of a tall clear glass of bubble milk tea, side view, distinct visible layers: "
                   "black tapioca pearls at bottom, brown sugar syrup drizzle on glass walls, "
                   "milk tea body, ice cubes, foam top. Studio lighting, hyper-realistic, 4K.",
     "expected": ["珍珠", "粉圆", "波霸", "糖浆", "奶茶", "茶", "冰", "tapioca", "pearl", "milk", "tea", "syrup"]},
]


# ============================================================
# Step 1: 生成源食物图（text-to-image，并行）
# ============================================================
def gen_source(food):
    """zeabur 只放行 /edits，所以用一张空白 1024² 白底当 ref，prompt 里要求重画。"""
    from PIL import Image
    blank = Image.new("RGB", (1024, 1024), (255, 255, 255))
    bb = io.BytesIO(); blank.save(bb, "PNG"); blank_png = bb.getvalue()

    boundary = "----src-" + uuid.uuid4().hex
    parts = [
        ("model", None, b"gpt-image-2", None),
        ("prompt", None, ("Ignore the reference and draw from scratch: " + food["gen_prompt"]).encode(), None),
        ("size", None, b"1024x1024", None),
        ("n", None, b"1", None),
        ("image", "blank.png", blank_png, "image/png"),
    ]
    body = io.BytesIO()
    for n, fn, v, ct in parts:
        body.write(("--" + boundary + "\r\n").encode())
        if fn:
            body.write(('Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (n, fn)).encode())
            body.write(("Content-Type: %s\r\n\r\n" % ct).encode())
        else:
            body.write(('Content-Disposition: form-data; name="%s"\r\n\r\n' % n).encode())
        body.write(v); body.write(b"\r\n")
    body.write(("--" + boundary + "--\r\n").encode())
    body = body.getvalue()

    req = urllib.request.Request(IMAGE_URL, data=body, method="POST", headers={
        "Authorization": "Bearer " + IMAGE_KEY,
        "Content-Type": "multipart/form-data; boundary=" + boundary,
        "Content-Length": str(len(body)),
    })
    print(f"  [{food['id']}] gen-src ...", flush=True)
    t0 = time.time()
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, context=ssl.create_default_context(),
                                        timeout=300) as resp:
                j = json.loads(resp.read())
            item = j.get("data", [{}])[0]
            b64 = item.get("b64_json")
            if not b64:
                if item.get("url"):
                    with urllib.request.urlopen(item["url"], timeout=180) as r:
                        png = r.read()
                else:
                    raise RuntimeError("no image data")
            else:
                png = base64.b64decode(b64)
            path = os.path.join(OUT_DIR, food["id"], "src.png")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "wb").write(png)
            dt = round(time.time() - t0, 1)
            print(f"  [{food['id']}] gen-src OK in {dt}s ({len(png)//1024}KB)", flush=True)
            food["src_path"] = path
            food["src_bytes"] = png
            return True
        except Exception as e:
            print(f"  [{food['id']}] gen-src attempt {attempt+1} fail: {type(e).__name__}: {e}", flush=True)
            time.sleep(3 * (attempt + 1))
    food["src_path"] = None
    return False


# ============================================================
# Step 2: 调我们的 /api/generate-explosion
# ============================================================
def gen_explosion(food):
    if not food.get("src_path"):
        return False
    print(f"  [{food['id']}] explosion ...", flush=True)
    t0 = time.time()
    req = urllib.request.Request(SERVER + "/api/generate-explosion",
                                 data=food["src_bytes"],
                                 headers={"Content-Type": "image/png"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            j = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        print(f"  [{food['id']}] explosion FAIL HTTP {e.code}: {body}", flush=True)
        return False
    except Exception as e:
        print(f"  [{food['id']}] explosion FAIL: {e}", flush=True)
        return False
    if not j.get("ok"):
        print(f"  [{food['id']}] explosion FAIL: {j}", flush=True)
        return False
    png = base64.b64decode(j["image_b64"])
    path = os.path.join(OUT_DIR, food["id"], "explosion.png")
    open(path, "wb").write(png)
    dt = round(time.time() - t0, 1)
    print(f"  [{food['id']}] explosion OK in {dt}s ({len(png)//1024}KB)", flush=True)
    food["exp_path"] = path
    food["exp_bytes"] = png
    return True


# ============================================================
# Step 3: step-3.6 批量解读
# ============================================================
def batch_explain(food):
    if not food.get("exp_path"):
        return None
    print(f"  [{food['id']}] step-3.6 batch ...", flush=True)
    client = OpenAI(api_key=STEPFUN_KEY, base_url="https://api.stepfun.com/v1")
    b64 = base64.b64encode(food["exp_bytes"]).decode()
    data_url = f"data:image/png;base64,{b64}"
    SYSTEM = """你是「食物解构师」。下方是一张食物的「垂直爆炸分解图」。

请从上到下列出图中所有可识别的成分层（通常 3-7 层），每层输出：
- index, name_zh, name_en, y_ratio_top, y_ratio_bottom（0-1 浮点）
- card: Markdown 科普卡（开头 ## 中文名(EN)，含一句话本质/起源故事/营养亮点/趣味提示四段）

严格输出 JSON 数组，无任何包裹文字。"""
    t0 = time.time()
    try:
        resp = client.chat.completions.create(
            model="step-3.6",
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": [
                    {"type": "text", "text": f"这是「{food['name_zh']}」的爆炸图，列出所有层。"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ]},
            ],
            timeout=300,
        )
    except Exception as e:
        print(f"  [{food['id']}] step-3.6 FAIL: {e}", flush=True)
        return None
    dt = round(time.time() - t0, 1)
    raw = (resp.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        if raw.endswith("```"): raw = raw.rsplit("```", 1)[0]
    raw = raw.strip()
    try:
        layers = json.loads(raw)
    except Exception as e:
        print(f"  [{food['id']}] step-3.6 解析失败 {dt}s: {e}\n  raw: {raw[:200]}", flush=True)
        return None
    open(os.path.join(OUT_DIR, food["id"], "layers.json"), "w").write(
        json.dumps(layers, ensure_ascii=False, indent=2))
    print(f"  [{food['id']}] step-3.6 OK in {dt}s, {len(layers)} 层", flush=True)
    return layers


# ============================================================
# 评分
# ============================================================
def score(food, layers):
    if not layers:
        return 0, "无结果"
    hits = []
    misses = []
    for kw in food["expected"]:
        if any(kw in (L.get("name_zh", "") + L.get("name_en", "")).lower()
               or kw.lower() in (L.get("name_zh", "") + L.get("name_en", "")).lower()
               for L in layers):
            hits.append(kw)
        else:
            misses.append(kw)
    return len(hits), f"命中 {len(hits)}/{len(food['expected'])}: hits={hits[:5]} miss={misses[:5]}"


# ============================================================
# 主流程：并行 source gen，串行后续
# ============================================================
def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print("=== Phase 1: 并行生成 4 张源食物图 ===")
    threads = [threading.Thread(target=gen_source, args=(f,)) for f in FOODS]
    [t.start() for t in threads]
    [t.join() for t in threads]
    print()

    print("=== Phase 2: 串行调 /api/generate-explosion + step-3.6 批量解读 ===")
    results = []
    for food in FOODS:
        if not food.get("src_path"):
            results.append((food["id"], None, "源图未生成"))
            continue
        if not gen_explosion(food):
            results.append((food["id"], None, "爆炸图失败"))
            continue
        layers = batch_explain(food)
        results.append((food["id"], layers, None))
        print()

    print("\n=== 汇总评分 ===")
    for fid, layers, fail in results:
        food = next(f for f in FOODS if f["id"] == fid)
        if fail:
            print(f"  {fid:12} FAIL: {fail}")
            continue
        n_hit, msg = score(food, layers)
        n_layers = len(layers) if layers else 0
        names = " / ".join((L.get("name_zh", "") for L in (layers or [])))
        print(f"  {fid:12} {n_layers} 层 | {msg}")
        print(f"               识出: {names}")


if __name__ == "__main__":
    main()

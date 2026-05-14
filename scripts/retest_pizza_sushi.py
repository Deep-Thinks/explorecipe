"""复用已有的 src.png，只重跑 pizza + sushi 的 explosion + explain，看新 prompt 是否修正布局。"""
import base64
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

from openai import OpenAI

OUT_DIR = "/Token-Exchange/temp/temp2/stress_v2"
SERVER = "http://localhost:18081"
env = {l.split("=", 1)[0].strip(): l.split("=", 1)[1].strip()
       for l in open("/niuniu869_dev/explorecipe/.env")
       if "=" in l and not l.startswith("#")}
STEPFUN_KEY = env["STEPFUN_API_KEY"]

TARGETS = [
    {"id": "pizza", "name_zh": "玛格丽特披萨", "src_path": "/Token-Exchange/temp/temp2/stress/pizza/src.png"},
    {"id": "sushi", "name_zh": "三文鱼寿司", "src_path": "/Token-Exchange/temp/temp2/stress/sushi/src.png"},
]


def explosion(food):
    src = open(food["src_path"], "rb").read()
    req = urllib.request.Request(SERVER + "/api/generate-explosion",
        data=src, headers={"Content-Type": "image/png"}, method="POST")
    print(f"[{food['id']}] explosion ...", end="", flush=True)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            j = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f" FAIL HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}")
        return None
    if not j.get("ok"):
        print(f" FAIL: {j}"); return None
    png = base64.b64decode(j["image_b64"])
    out = os.path.join(OUT_DIR, food["id"], "explosion.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "wb").write(png)
    print(f" OK {round(time.time()-t0, 1)}s -> {out}")
    return png


def explain(food, png):
    client = OpenAI(api_key=STEPFUN_KEY, base_url="https://api.stepfun.com/v1")
    b64 = base64.b64encode(png).decode()
    data_url = f"data:image/png;base64,{b64}"
    SYSTEM = """你是「食物解构师」。下方是一张食物的「垂直爆炸分解图」。

请从上到下列出图中所有可识别的成分层（通常 3-7 层），每层输出：
- index, name_zh, name_en, y_ratio_top, y_ratio_bottom（0-1 浮点）
- card: Markdown 科普卡（开头 ## 中文名(EN)，含一句话本质/起源故事/营养亮点/趣味提示四段）

严格输出 JSON 数组。"""
    print(f"[{food['id']}] step-3.6 batch ...", end="", flush=True)
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
        print(f" FAIL: {e}"); return None
    raw = (resp.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        if raw.endswith("```"): raw = raw.rsplit("```", 1)[0]
    raw = raw.strip()
    try:
        layers = json.loads(raw)
    except Exception as e:
        print(f" 解析失败 {e}, raw: {raw[:200]}"); return None
    print(f" OK {round(time.time()-t0, 1)}s, {len(layers)} 层")
    open(os.path.join(OUT_DIR, food["id"], "layers.json"), "w").write(
        json.dumps(layers, ensure_ascii=False, indent=2))
    return layers


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for food in TARGETS:
        png = explosion(food)
        if png is None: continue
        layers = explain(food, png)
        if layers:
            names = " / ".join(L.get("name_zh", "") for L in layers)
            print(f"  -> {names}\n")


if __name__ == "__main__":
    main()

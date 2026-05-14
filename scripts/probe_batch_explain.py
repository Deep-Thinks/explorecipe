"""一次调 step-3.6 拿全部 6 层介绍 + bbox 的方案验证。"""
import base64
import io
import json
import time

from openai import OpenAI

key = [l.split("=", 1)[1].strip()
       for l in open("/niuniu869_dev/explorecipe/.env")
       if l.startswith("STEPFUN_API_KEY=")][0]
client = OpenAI(api_key=key, base_url="https://api.stepfun.com/v1")

img_bytes = open("/Token-Exchange/temp/temp2/explosion.png", "rb").read()
b64 = base64.b64encode(img_bytes).decode()
data_url = f"data:image/png;base64,{b64}"

SYSTEM = """你是「食物解构师」。下方是一张食物的「垂直爆炸分解图」——食物的各成分被竖向分层悬浮展示。

请从上到下列出图中**所有可识别的成分层**（通常 4-7 层），为每一层输出：
- `index`: 从 0 开始的层序号（最上面是 0）
- `name_zh`: 中文成分名
- `name_en`: 英文成分名
- `y_ratio_top`: 该层在图中**上边缘**的相对位置（0.0=顶 ~ 1.0=底，浮点）
- `y_ratio_bottom`: 该层**下边缘**的相对位置
- `card`: Markdown 科普卡，结构：开头一行 `## 中文名(EN)`，然后 **一句话本质** / **起源故事** / **营养亮点** / **趣味提示** 四段，每段 30-60 字。不要编精确营养数字。

**严格输出 JSON 格式**，外层是一个数组，不要任何额外解释文字、不要 markdown 代码块包裹。例子：

[
  {"index": 0, "name_zh": "面包顶", "name_en": "Brioche Bun Top",
   "y_ratio_top": 0.04, "y_ratio_bottom": 0.22,
   "card": "## 面包顶(Brioche Bun Top)\\n\\n**一句话本质**：..."},
  ...
]
"""

print("调 step-3.6 全图批量识别 ...", flush=True)
t0 = time.time()
resp = client.chat.completions.create(
    model="step-3.6",
    messages=[
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": [
            {"type": "text", "text": "列出图中所有成分层。"},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]},
    ],
    timeout=300,
    response_format={"type": "json_object"} if False else None,  # 先不强制，看 step-3.6 给不给
)
dt = round(time.time() - t0, 1)
raw = resp.choices[0].message.content or ""
print(f"耗时 {dt}s, 返回 {len(raw)} 字符")
print("\n--- raw output (前 500 字符) ---")
print(raw[:500])
print("...")

# 尝试解析 JSON
clean = raw.strip()
if clean.startswith("```"):
    clean = clean.split("\n", 1)[1] if "\n" in clean else clean
    if clean.endswith("```"):
        clean = clean.rsplit("```", 1)[0]
clean = clean.strip()
# 兜底：如果是对象包裹
if clean.startswith("{") and not clean.startswith("["):
    # 找数组字段
    try:
        obj = json.loads(clean)
        for k, v in obj.items():
            if isinstance(v, list):
                clean = json.dumps(v); break
    except: pass

try:
    layers = json.loads(clean)
    print(f"\n--- 解析 OK，共 {len(layers)} 层 ---")
    for L in layers:
        print(f"  #{L.get('index')} y∈[{L.get('y_ratio_top'):.2f},{L.get('y_ratio_bottom'):.2f}] "
              f"{L.get('name_zh')}({L.get('name_en')})")
        # 只显示 card 第一行
        card = L.get("card", "")
        print(f"    {card.splitlines()[0] if card else ''}")
    # 落盘
    open("/Token-Exchange/temp/temp2/layers.json", "w").write(json.dumps(layers, ensure_ascii=False, indent=2))
    print("\n保存到 layers.json")
except Exception as e:
    print(f"\n--- 解析失败 {e} ---")
    print(raw)

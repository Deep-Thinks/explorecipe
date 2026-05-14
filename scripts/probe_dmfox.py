"""绕过我们的 server，直接打 dm-fox：用最小输入排查到底是上游、模型、prompt 还是 ref 图的问题。"""
import io, json, ssl, time, urllib.request, uuid, os, sys
from PIL import Image

URL = "https://dm-fox.rjj.cc/gptapi/v1/images/edits"
KEY = open("/niuniu869_dev/lilibear_world/.env").read()
KEY = [l.split("=", 1)[1].strip() for l in KEY.splitlines() if l.startswith("IMAGE_API_KEY=")][0]


def build_multipart(boundary, fields):
    out = io.BytesIO()
    for item in fields:
        name, filename, value, ctype = item if len(item) == 4 else (*item, None)
        out.write(("--" + boundary + "\r\n").encode())
        if filename:
            out.write(('Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (name, filename)).encode())
            out.write(("Content-Type: %s\r\n\r\n" % (ctype or "application/octet-stream")).encode())
        else:
            out.write(('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode())
        if isinstance(value, str):
            value = value.encode("utf-8")
        out.write(value)
        out.write(b"\r\n")
    out.write(("--" + boundary + "--\r\n").encode())
    return out.getvalue()


def try_case(name, model, prompt, size, img_bytes, img_mime, img_ext):
    boundary = "----probe-" + uuid.uuid4().hex
    fields = [
        ("model", None, model.encode(), None),
        ("prompt", None, prompt.encode(), None),
        ("n", None, b"1", None),
        ("image", "ref." + img_ext, img_bytes, img_mime),
    ]
    if size:
        fields.insert(2, ("size", None, size.encode(), None))

    body = build_multipart(boundary, fields)
    headers = {
        "Authorization": "Bearer " + KEY,
        "Content-Type": "multipart/form-data; boundary=" + boundary,
        "Content-Length": str(len(body)),
    }
    req = urllib.request.Request(URL, data=body, headers=headers, method="POST")
    print(f"\n[case {name}] model={model} size={size} prompt={prompt[:50]!r} ref={len(img_bytes)/1024:.0f}KB ", end="", flush=True)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=300) as resp:
            raw = resp.read()
        j = json.loads(raw.decode("utf-8"))
        elapsed = round(time.time() - t0, 1)
        if j.get("data") and j["data"][0].get("b64_json"):
            print(f"OK in {elapsed}s, returned image {len(j['data'][0]['b64_json']) * 3 // 4} bytes")
            return j["data"][0]["b64_json"]
        elif j.get("data") and j["data"][0].get("url"):
            print(f"OK in {elapsed}s, url={j['data'][0]['url'][:60]}...")
            return j["data"][0]["url"]
        else:
            print(f"NO IMAGE in {elapsed}s: {str(j)[:300]}")
            return None
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}")
        return None
    except Exception as e:
        print(f"{type(e).__name__}: {e}")
        return None


def main():
    # 1) 用 lilibear 已知能跑通的 ref 图作为基线（最小、压缩过）
    li_refs_dir = "/niuniu869_dev/lilibear_world/refs_compressed"
    li_refs = sorted(os.listdir(li_refs_dir))
    li_ref0 = os.path.join(li_refs_dir, li_refs[0])
    li_ref0_bytes = open(li_ref0, "rb").read()
    print(f"lilibear baseline ref: {li_ref0} ({len(li_ref0_bytes)/1024:.0f}KB)")

    # 2) 我们的食物图（压到小尺寸）
    food_raw = open("/Token-Exchange/temp/微信图片_20260514121041_365_90.jpg", "rb").read()
    img = Image.open(io.BytesIO(food_raw)).convert("RGB")
    img.thumbnail((1024, 1024), Image.LANCZOS)
    buf = io.BytesIO(); img.save(buf, "JPEG", quality=85)
    food_small = buf.getvalue()
    print(f"food test img: {img.size} ({len(food_small)/1024:.0f}KB)")

    # case A: lilibear baseline——证明 key/路由/模型在线
    try_case("A-baseline-li-ref",
             model="gpt-image-2",
             prompt="Make a cute mascot drawing in watercolor style.",
             size="1024x1024",
             img_bytes=li_ref0_bytes, img_mime="image/jpeg", img_ext="jpg")

    # case B: 食物图 + 极简 prompt + 标准尺寸
    try_case("B-food-simple-prompt",
             model="gpt-image-2",
             prompt="Show the dish in this image floating against a dark background.",
             size="1024x1024",
             img_bytes=food_small, img_mime="image/jpeg", img_ext="jpg")

    # case C: 食物图 + 我们的复杂 prompt + 标准尺寸
    EXPLOSION_PROMPT = open("/niuniu869_dev/explorecipe/server.py").read()
    # 提取 EXPLOSION_PROMPT 字符串字面量
    start = EXPLOSION_PROMPT.find('EXPLOSION_PROMPT = """') + len('EXPLOSION_PROMPT = """')
    end = EXPLOSION_PROMPT.find('"""', start)
    real_prompt = EXPLOSION_PROMPT[start:end]
    try_case("C-food-full-prompt",
             model="gpt-image-2",
             prompt=real_prompt,
             size="1024x1024",
             img_bytes=food_small, img_mime="image/jpeg", img_ext="jpg")

    # case D: 食物图 + 复杂 prompt + 竖版尺寸
    try_case("D-food-full-prompt-vertical",
             model="gpt-image-2",
             prompt=real_prompt,
             size="1024x1536",
             img_bytes=food_small, img_mime="image/jpeg", img_ext="jpg")


if __name__ == "__main__":
    main()

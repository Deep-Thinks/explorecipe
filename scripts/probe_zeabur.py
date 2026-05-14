"""测试 chatgpt2api2.zeabur.app 接口的稳定性。"""
import io
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid

from PIL import Image, ImageDraw

# key 从环境变量取，不写入文件——临时验证用
KEY = os.environ.get("PROBE_KEY", "")
assert KEY, "需要 PROBE_KEY"

BASE = "https://chatgpt2api2.zeabur.app"


def build_multipart(boundary, parts):
    out = io.BytesIO()
    for name, fn, value, ctype in parts:
        out.write(("--" + boundary + "\r\n").encode())
        if fn:
            out.write(('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                       % (name, fn)).encode())
            out.write(("Content-Type: %s\r\n\r\n" % ctype).encode())
        else:
            out.write(('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode())
        if isinstance(value, str):
            value = value.encode()
        out.write(value)
        out.write(b"\r\n")
    out.write(("--" + boundary + "--\r\n").encode())
    return out.getvalue()


def make_synthetic():
    img = Image.new("RGB", (1024, 1024), (200, 150, 100))
    ImageDraw.Draw(img).ellipse([200, 200, 824, 824], fill=(80, 200, 80))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def call_edit(case_name, prompt, ref_bytes, ref_mime, model="gpt-image-2", size="1024x1024"):
    boundary = "----zb-" + uuid.uuid4().hex
    body = build_multipart(boundary, [
        ("model", None, model, None),
        ("prompt", None, prompt, None),
        ("size", None, size, None),
        ("n", None, "1", None),
        ("image", "ref." + ref_mime.split("/")[-1], ref_bytes, ref_mime),
    ])
    url = BASE + "/v1/images/edits"
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": "Bearer " + KEY,
        "Content-Type": "multipart/form-data; boundary=" + boundary,
        "Content-Length": str(len(body)),
    })
    print(f"\n[{case_name}] {url} model={model} ref={len(ref_bytes)//1024}KB ... ",
          end="", flush=True)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, context=ssl.create_default_context(),
                                    timeout=240) as resp:
            raw = resp.read()
        dt = round(time.time() - t0, 1)
        try:
            j = json.loads(raw.decode("utf-8"))
        except Exception:
            print(f"NON-JSON {dt}s -> {raw[:200]!r}")
            return None
        if j.get("data") and (j["data"][0].get("b64_json") or j["data"][0].get("url")):
            sz = len(j["data"][0].get("b64_json", "") or "") * 3 // 4
            print(f"OK {dt}s, img ~{sz//1024}KB")
            return j["data"][0]
        print(f"NO-IMG {dt}s -> {str(j)[:300]}")
        return None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:280]
        print(f"HTTP {e.code}: {body}")
        return None
    except Exception as e:
        print(f"{type(e).__name__}: {e}")
        return None


def main():
    syn = make_synthetic()

    # 1) 三次 synthetic + 简单 prompt：看基线稳定性
    print("=== Phase 1: synthetic × 3 ===")
    for i in range(3):
        call_edit(f"syn-{i}",
                  prompt="Make the green circle blue.",
                  ref_bytes=syn, ref_mime="image/png")

    # 2) 汉堡真测（先压缩）
    print("\n=== Phase 2: real food image ===")
    food = open("/Token-Exchange/temp/微信图片_20260514121041_365_90.jpg", "rb").read()
    img = Image.open(io.BytesIO(food)).convert("RGB")
    w, h = img.size
    s = min(w, h)
    img = img.crop(((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2))
    img = img.resize((1024, 1024), Image.LANCZOS)
    bb = io.BytesIO(); img.save(bb, "JPEG", quality=88)
    food_small = bb.getvalue()

    EXPLOSION = ("Create a hyper-realistic vertical exploded-view of the food in this image. "
                 "Identify its 4-6 actual ingredients and float each one as a separate layer "
                 "stacked vertically with clear gaps between them, on a dark graphite gradient "
                 "background. Each ingredient has a thin neon magenta outline (color #FF00FF) "
                 "and an uppercase English label to its left connected by a thin magenta line. "
                 "Studio lighting, sharp macro detail, high-end infographic style.")

    item = call_edit("food-explosion",
                     prompt=EXPLOSION,
                     ref_bytes=food_small, ref_mime="image/jpeg",
                     size="1024x1536")
    if item:
        import base64
        b64 = item.get("b64_json")
        if b64:
            out_path = "/niuniu869_dev/explorecipe/logs/e2e/explosion_zeabur.png"
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            open(out_path, "wb").write(base64.b64decode(b64))
            print(f"  saved -> {out_path}")
        elif item.get("url"):
            print(f"  url -> {item['url'][:80]}...")


if __name__ == "__main__":
    main()

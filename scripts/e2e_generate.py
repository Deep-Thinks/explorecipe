"""E2E Phase 1：上传食物图（先压缩） → 拿回爆炸分解图 → 落盘。"""
import base64
import io
import json
import os
import sys
import time
import urllib.request

TEST_IMG = "/Token-Exchange/temp/微信图片_20260514121041_365_90.jpg"
OUT_DIR = "/niuniu869_dev/explorecipe/logs/e2e"
SERVER = "http://localhost:18081"
MAX_SIDE = 1024  # gpt-image-2 ref 图压到长边 1024


def resize_jpeg(raw_bytes, max_side):
    """中心裁切成正方形 + 缩到 max_side。gpt-image-2 /edits 对非正方形 ref 似乎有问题。"""
    from PIL import Image
    img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    w, h = img.size
    s = min(w, h)
    img = img.crop(((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2))
    img = img.resize((max_side, max_side), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=88)
    return out.getvalue(), img.size


def main():
    raw = open(TEST_IMG, "rb").read()
    small, dim = resize_jpeg(raw, MAX_SIDE)
    print(f"input: {TEST_IMG} ({len(raw)/1024:.0f}KB) -> 压缩到 {dim} ({len(small)/1024:.0f}KB)")
    os.makedirs(OUT_DIR, exist_ok=True)
    open(os.path.join(OUT_DIR, "input_compressed.jpg"), "wb").write(small)

    print("POST /api/generate-explosion ...")
    sys.stdout.flush()
    t0 = time.time()
    req = urllib.request.Request(
        SERVER + "/api/generate-explosion",
        data=small,
        headers={"Content-Type": "image/jpeg"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            j = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code}: {body[:500]}")
        sys.exit(1)
    elapsed = round(time.time() - t0, 1)

    if not j.get("ok"):
        print("FAIL:", j)
        sys.exit(1)

    img_bytes = base64.b64decode(j["image_b64"])
    ext = "png" if j["mime"] == "image/png" else "jpg"
    out_path = os.path.join(OUT_DIR, f"explosion.{ext}")
    open(out_path, "wb").write(img_bytes)
    print(f"OK in {elapsed}s -> {out_path} ({len(img_bytes)/1024:.0f}KB)")


if __name__ == "__main__":
    main()

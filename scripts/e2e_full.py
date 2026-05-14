"""完整 E2E：上传 → 生成爆炸图 → 在 6 个层位点击 → 调 stepfun 拿每层科普卡。

镜像前端 JS 的 flood-fill 逻辑（品红描边为边界），让点击效果可在 Python 侧复现。
"""
import base64
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

from PIL import Image

TEST_IMG = "/Token-Exchange/temp/微信图片_20260514121041_365_90.jpg"
OUT_DIR = "/Token-Exchange/temp/temp2"
SERVER = "http://localhost:18081"
EXPLOSION_PATH = os.path.join(OUT_DIR, "explosion.png")


# ============================================================
# Phase 1: 生成爆炸图（经我们的 server，验证整合路径）
# ============================================================
def resize_square(raw, side=1024):
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    w, h = img.size
    s = min(w, h)
    img = img.crop(((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2))
    img = img.resize((side, side), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88)
    return out.getvalue()


def phase1_generate():
    os.makedirs(OUT_DIR, exist_ok=True)
    print("Phase 1: 生成爆炸图")
    raw = open(TEST_IMG, "rb").read()
    small = resize_square(raw)
    print(f"  upload {len(small)/1024:.0f}KB ... ", end="", flush=True)
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
        print(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}")
        sys.exit(1)
    dt = round(time.time() - t0, 1)
    if not j.get("ok"):
        print(f"FAIL ({dt}s): {j}")
        sys.exit(1)
    png = base64.b64decode(j["image_b64"])
    open(EXPLOSION_PATH, "wb").write(png)
    print(f"OK in {dt}s, saved -> {EXPLOSION_PATH} ({len(png)/1024:.0f}KB)")
    return Image.open(io.BytesIO(png)).convert("RGB")


# ============================================================
# Phase 2: flood-fill 6 个点
# ============================================================
def is_outline_pixel(r, g, b):
    return r > 200 and g < 90 and b > 200


def flood_fill_from(img, sx, sy):
    """返回 (mask_2d, bbox) 或 None。镜像前端 JS 的 scanline 实现。"""
    px = img.load()
    W, H = img.size
    if not (0 <= sx < W and 0 <= sy < H):
        return None
    r, g, b = px[sx, sy][:3]
    if is_outline_pixel(r, g, b):
        return None
    max_pixels = int(W * H * 0.35)
    visited = bytearray(W * H)
    mask = bytearray(W * H)
    count = 0
    minx, miny, maxx, maxy = sx, sy, sx, sy
    stack = [(sx, sy)]
    visited[sy * W + sx] = 1
    while stack:
        x, y = stack.pop()
        # 向左
        lx = x
        while lx > 0:
            rr, gg, bb = px[lx - 1, y][:3]
            if is_outline_pixel(rr, gg, bb):
                break
            lx -= 1
        # 向右
        rx = x
        while rx < W - 1:
            rr, gg, bb = px[rx + 1, y][:3]
            if is_outline_pixel(rr, gg, bb):
                break
            rx += 1
        for xi in range(lx, rx + 1):
            k = y * W + xi
            if not mask[k]:
                mask[k] = 1
                count += 1
                if count > max_pixels:
                    return None
                if xi < minx: minx = xi
                if xi > maxx: maxx = xi
                if y  < miny: miny = y
                if y  > maxy: maxy = y
        for dy in (-1, 1):
            ny = y + dy
            if not (0 <= ny < H): continue
            in_seg = False
            for xi in range(lx, rx + 1):
                k = ny * W + xi
                rr, gg, bb = px[xi, ny][:3]
                if not is_outline_pixel(rr, gg, bb) and not visited[k]:
                    if not in_seg:
                        stack.append((xi, ny))
                        visited[k] = 1
                        in_seg = True
                elif is_outline_pixel(rr, gg, bb):
                    in_seg = False
    if count < 400:
        return None
    return mask, (minx, miny, maxx + 1, maxy + 1), count


def crop_with_padding(img, bbox, pad=8):
    x0, y0, x1, y1 = bbox
    W, H = img.size
    x0 = max(0, x0 - pad); y0 = max(0, y0 - pad)
    x1 = min(W, x1 + pad); y1 = min(H, y1 + pad)
    return img.crop((x0, y0, x1, y1))


# ============================================================
# Phase 3: 调 stepfun
# ============================================================
def explain(crop):
    buf = io.BytesIO()
    crop.save(buf, "PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    payload = json.dumps({"image_b64": b64}).encode()
    req = urllib.request.Request(
        SERVER + "/api/explain-region",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            j = json.loads(resp.read())
    except Exception as e:
        return None, str(e), 0
    dt = round(time.time() - t0, 1)
    if not j.get("ok"):
        return None, str(j)[:300], dt
    return j.get("markdown", ""), None, dt


# ============================================================
# 主流程
# ============================================================
def main():
    img = phase1_generate()
    W, H = img.size
    print(f"\nPhase 2 & 3: 在生成图 {W}x{H} 上点 6 层 + 调 stepfun")
    # 6 个层位的猜测中心点（看实际生成图，食物层右移，标签在左）
    # 用相对比例方便不同 size 通用
    POINTS = [
        ("L0-BUN_TOP",    0.62, 0.12),
        ("L1-FOIE_GRAS",  0.62, 0.27),
        ("L2-BEEF_PATTY", 0.62, 0.42),
        ("L3-LETTUCE",    0.62, 0.57),
        ("L4-SAUCE",      0.62, 0.72),
        ("L5-BUN_BOT",    0.62, 0.87),
    ]
    for name, rx, ry in POINTS:
        x, y = int(W * rx), int(H * ry)
        print(f"\n=== {name} (click @ {x},{y}) ===")
        res = flood_fill_from(img, x, y)
        if res is None:
            print("  flood-fill FAIL（点在描边/背景上，或区域过小）")
            continue
        mask, bbox, count = res
        print(f"  mask {count}px, bbox {bbox}")
        crop = crop_with_padding(img, bbox)
        crop_path = os.path.join(OUT_DIR, f"crop_{name}.png")
        crop.save(crop_path)
        print(f"  saved crop -> {crop_path}")
        md, err, dt = explain(crop)
        if md is None:
            print(f"  stepfun FAIL ({dt}s): {err}")
            continue
        print(f"  stepfun OK ({dt}s):\n{md.strip()}")


if __name__ == "__main__":
    main()

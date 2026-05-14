"""E2E Phase 2 (独立验证)：dm-fox 当下挂掉，绕过它直接验证 stepfun 视觉模式。

策略：把用户原图（鹅肝美式汉堡）按九宫格切成 9 块，每块独立发给 stepfun，
看 step-3.6 能不能识别出汉堡的各组成（顶层、肉饼、鹅肝、生菜、底层等）。
"""
import base64
import io
import json
import os
import sys
import time
import urllib.request

from PIL import Image

TEST_IMG = "/Token-Exchange/temp/微信图片_20260514121041_365_90.jpg"
OUT_DIR = "/niuniu869_dev/explorecipe/logs/e2e"
SERVER = "http://localhost:18081"

# 鹅肝美式汉堡通常是垂直堆叠的结构，竖向切 5 段比 3x3 更合理
N_SLICES = 5


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    img = Image.open(TEST_IMG).convert("RGB")
    W, H = img.size
    print(f"input: {TEST_IMG}  size={W}x{H}")

    slices = []
    for i in range(N_SLICES):
        y0 = int(H * i / N_SLICES)
        y1 = int(H * (i + 1) / N_SLICES)
        # 水平裁中央 70% 减少边缘背景干扰
        x0 = int(W * 0.15)
        x1 = int(W * 0.85)
        s = img.crop((x0, y0, x1, y1))
        # 压成 PNG（不超过 400KB）
        buf = io.BytesIO()
        s.thumbnail((900, 900), Image.LANCZOS)
        s.save(buf, "PNG", optimize=True)
        sp = os.path.join(OUT_DIR, f"slice_{i}.png")
        open(sp, "wb").write(buf.getvalue())
        slices.append((sp, buf.getvalue()))
        print(f"  slice {i}: y∈[{y0},{y1}] saved={len(buf.getvalue())//1024}KB")

    print("\n--- 调 step-3.6 ---")
    for i, (sp, sbytes) in enumerate(slices):
        b64 = base64.b64encode(sbytes).decode("ascii")
        payload = json.dumps({"image_b64": b64}).encode()
        req = urllib.request.Request(
            SERVER + "/api/explain-region",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                j = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            print(f"\n=== slice {i} === FAIL HTTP {e.code}: {body[:300]}")
            continue
        except Exception as e:
            print(f"\n=== slice {i} === FAIL {type(e).__name__}: {e}")
            continue
        dt = round(time.time() - t0, 1)
        if not j.get("ok"):
            print(f"\n=== slice {i} === ({dt}s) FAIL: {j}")
            continue
        md = j.get("markdown", "")
        print(f"\n=== slice {i} (top→bottom #{i+1}/{N_SLICES}, {dt}s) ===")
        print(md.strip())


if __name__ == "__main__":
    main()

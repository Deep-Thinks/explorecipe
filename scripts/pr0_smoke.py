"""PR-0 烟雾测试：验证 1024x1792 (9:16) 是否被 token-recyclebin 接受。

跑法：
  python3 scripts/pr0_smoke.py 1024x1792
  python3 scripts/pr0_smoke.py 1024x1536  # 回退基准
"""
import base64
import io
import json
import os
import ssl
import sys
import time
import urllib.request
import uuid

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


def _multipart(boundary, fields):
    out = io.BytesIO()
    for name, fn, value, ctype in fields:
        out.write(("--" + boundary + "\r\n").encode())
        if fn:
            out.write(('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                       % (name, fn)).encode())
            out.write(("Content-Type: %s\r\n\r\n" % ctype).encode())
        else:
            out.write(('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode())
        if isinstance(value, str):
            value = value.encode("utf-8")
        out.write(value)
        out.write(b"\r\n")
    out.write(("--" + boundary + "--\r\n").encode())
    return out.getvalue()


def main():
    size = sys.argv[1] if len(sys.argv) > 1 else "1024x1792"
    sample = os.environ.get(
        "SMOKE_IMG",
        "/niuniu869_dev/explorecipe/logs/images/20260514/181743_35fa367d.png",
    )
    with open(sample, "rb") as f:
        img = f.read()
    print("[smoke] image=%s size=%s bytes=%d" % (sample, size, len(img)))

    boundary = "----smoke-" + uuid.uuid4().hex
    body = _multipart(boundary, [
        ("model", None, "gpt-image-2", "text/plain"),
        ("prompt", None,
         "Create a simple vertical exploded view of the food. Stack 3 items vertically. "
         "Use a wooden table background. Output a clean photo.",
         "text/plain"),
        ("size", None, size, "text/plain"),
        ("n", None, "1", "text/plain"),
        ("image", "food.png", img, "image/png"),
    ])
    headers = {
        "Authorization": "Bearer " + os.environ["IMAGE_API_KEY"],
        "Content-Type": "multipart/form-data; boundary=" + boundary,
        "Content-Length": str(len(body)),
    }
    url = os.environ.get("IMAGE_UPSTREAM_URL",
                         "https://image.token-recyclebin.com/v1/images/edits")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    ctx = ssl.create_default_context()
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=300) as resp:
            raw = resp.read()
        j = json.loads(raw.decode("utf-8"))
        if "data" in j and j["data"]:
            item = j["data"][0]
            b64 = item.get("b64_json")
            if b64:
                out = base64.b64decode(b64)
                outpath = os.path.join(
                    ROOT, "logs", "pr0_smoke_%s.png" % size.replace("x", "_"))
                with open(outpath, "wb") as f:
                    f.write(out)
                print("[smoke] OK size=%s elapsed=%.1fs -> %s (%d bytes)"
                      % (size, time.time() - t0, outpath, len(out)))
                return 0
        print("[smoke] FAIL no data: %s" % str(j)[:300])
        return 1
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        print("[smoke] HTTPError %d size=%s: %s" % (e.code, size, body_text))
        return 1
    except Exception as e:
        print("[smoke] ERR size=%s: %s: %s" % (size, type(e).__name__, e))
        return 1


if __name__ == "__main__":
    sys.exit(main())

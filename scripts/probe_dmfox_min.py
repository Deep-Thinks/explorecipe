"""最小变量探针：定位 dm-fox 当下能跑通的最简组合。"""
import io, json, ssl, time, urllib.request, uuid, os, sys
from PIL import Image, ImageDraw

URL = "https://dm-fox.rjj.cc/gptapi/v1/images/edits"
KEY = [l.split("=", 1)[1].strip() for l in open("/niuniu869_dev/lilibear_world/.env") if l.startswith("IMAGE_API_KEY=")][0]


def make_tiny_image():
    """生成一张极简 512x512 PNG。"""
    img = Image.new("RGB", (512, 512), (255, 220, 180))
    d = ImageDraw.Draw(img)
    d.ellipse([100, 100, 412, 412], fill=(220, 80, 60))
    buf = io.BytesIO(); img.save(buf, "PNG")
    return buf.getvalue()


def build_multipart(boundary, fields):
    out = io.BytesIO()
    for name, fn, value, ctype in fields:
        out.write(("--" + boundary + "\r\n").encode())
        if fn:
            out.write(('Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (name, fn)).encode())
            out.write(("Content-Type: %s\r\n\r\n" % (ctype or "application/octet-stream")).encode())
        else:
            out.write(('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode())
        if isinstance(value, str): value = value.encode()
        out.write(value); out.write(b"\r\n")
    out.write(("--" + boundary + "--\r\n").encode())
    return out.getvalue()


def call(case, fields):
    boundary = "----p-" + uuid.uuid4().hex
    body = build_multipart(boundary, fields)
    headers = {"Authorization": "Bearer " + KEY,
               "Content-Type": "multipart/form-data; boundary=" + boundary,
               "Content-Length": str(len(body))}
    req = urllib.request.Request(URL, data=body, headers=headers, method="POST")
    print(f"\n[{case}] body={len(body)}B ", end="", flush=True)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=180) as resp:
            raw = resp.read()
        j = json.loads(raw.decode("utf-8"))
        dt = round(time.time() - t0, 1)
        if j.get("data") and (j["data"][0].get("b64_json") or j["data"][0].get("url")):
            sz = len(j["data"][0].get("b64_json","") or "") * 3 // 4
            print(f"OK {dt}s -> img {sz}B"); return j
        print(f"NO-IMG {dt}s -> {str(j)[:280]}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:280]
        print(f"HTTP {e.code}: {body}")
    except Exception as e:
        print(f"{type(e).__name__}: {e}")
    return None


def main():
    img = make_tiny_image()
    open("/niuniu869_dev/explorecipe/logs/e2e/probe_input.png", "wb").write(img)

    # M1: 最小 - 只有 image + prompt + model
    call("M1-bare", [
        ("model", None, "gpt-image-2", None),
        ("prompt", None, "Add a small green leaf on top of the circle.", None),
        ("image", "in.png", img, "image/png"),
    ])

    # M2: 加 size
    call("M2-+size", [
        ("model", None, "gpt-image-2", None),
        ("prompt", None, "Add a small green leaf on top of the circle.", None),
        ("size", None, "1024x1024", None),
        ("image", "in.png", img, "image/png"),
    ])

    # M3: 试 gpt-image-1 (老模型，许多代理只支持这个)
    call("M3-gpt-image-1", [
        ("model", None, "gpt-image-1", None),
        ("prompt", None, "Add a small green leaf on top of the circle.", None),
        ("size", None, "1024x1024", None),
        ("image", "in.png", img, "image/png"),
    ])

    # M4: 改用 jpeg ref
    img_jpeg_buf = io.BytesIO()
    Image.open(io.BytesIO(img)).convert("RGB").save(img_jpeg_buf, "JPEG", quality=90)
    img_jpeg = img_jpeg_buf.getvalue()
    call("M4-jpeg-ref-gpt-image-2", [
        ("model", None, "gpt-image-2", None),
        ("prompt", None, "Add a small green leaf on top of the circle.", None),
        ("size", None, "1024x1024", None),
        ("image", "in.jpg", img_jpeg, "image/jpeg"),
    ])


if __name__ == "__main__":
    main()

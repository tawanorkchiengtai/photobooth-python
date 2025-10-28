import io
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

from flask import Flask, Response, jsonify, request, send_from_directory, render_template, send_file
from PIL import Image, ImageOps

app = Flask(__name__, static_folder="static", template_folder="templates")

PHOTOS_DIR = Path(os.environ.get("PHOTOBOOTH_PHOTOS_DIR", str(Path.home() / "photobooth/data/photos")))
TEMPLATES_PATH = Path(os.environ.get("PHOTOBOOTH_TEMPLATES_PATH", str(Path(__file__).parent / "public/templates/index.json")))
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

BOUNDARY = "frame"


def extract_jpegs(buffer: bytearray) -> List[bytes]:
    frames: List[bytes] = []
    i = 0
    n = len(buffer)
    while i + 1 < n:
        if buffer[i] == 0xFF and buffer[i + 1] == 0xD8:  # SOI
            j = i + 2
            while j + 1 < n:
                if buffer[j] == 0xFF and buffer[j + 1] == 0xD9:  # EOI
                    j += 2
                    frames.append(bytes(buffer[i:j]))
                    i = j
                    break
                j += 1
            else:
                break
        i += 1
    if i > 0:
        del buffer[:i]
    return frames


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/stream")
def stream():
    def generate():
        proc = subprocess.Popen(
            [
                "rpicam-vid",
                "-n",
                "--codec",
                "mjpeg",
                "--width",
                "960",
                "--height",
                "540",
                "--framerate",
                "15",
                "--quality",
                "65",
                "-t",
                "0",
                "-o",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        buf = bytearray()
        try:
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                buf.extend(chunk)
                for frame in extract_jpegs(buf):
                    part = (
                        f"--{BOUNDARY}\r\n"
                        f"Content-Type: image/jpeg\r\n"
                        f"Content-Length: {len(frame)}\r\n\r\n"
                    ).encode("ascii") + frame + b"\r\n"
                    yield part
        finally:
            try:
                proc.terminate()
            except Exception:
                pass
    return Response(generate(), mimetype=f"multipart/x-mixed-replace; boundary={BOUNDARY}")


@app.post("/capture")
def capture():
    width = str(request.json.get("width", 1920))
    height = str(request.json.get("height", 1080))
    ts = datetime.utcnow().strftime("%Y/%m/%d/%H%M%S_%f")
    out_path = PHOTOS_DIR / f"{ts}.jpg"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "rpicam-still",
            "-n",
            "-o",
            str(out_path),
            "--width",
            width,
            "--height",
            height,
            "-t",
            "1",
            "-q",
            "95",
        ]
    )
    if proc.returncode != 0:
        return jsonify({"ok": False, "error": "rpicam-still failed"}), 500
    return jsonify({"ok": True, "path": str(out_path), "url": f"/photo/{ts}.jpg"})


@app.get("/photo/<path:rel>")
def get_photo(rel: str):
    rel_path = Path(rel)
    return send_from_directory(PHOTOS_DIR / rel_path.parent, rel_path.name)


@app.post("/compose")
def compose():
    data = request.get_json(force=True)
    selected_paths: List[str] = data.get("selected_paths", [])
    filt = data.get("filter", "none")
    template_id = data.get("template_id", "single_full")

    try:
        templates = json.loads(Path(TEMPLATES_PATH).read_text())
    except Exception as e:
        return jsonify({"ok": False, "error": f"failed to read templates: {e}"}), 500
    tpl = next((t for t in templates if t.get("id") == template_id), None)
    if not tpl:
        return jsonify({"ok": False, "error": f"template {template_id} not found"}), 400

    W, H = 2480, 3508
    
    # Load background template if available
    background_path = tpl.get("background")
    if background_path and Path(background_path).exists():
        try:
            canvas = Image.open(background_path).convert("RGB")
            # Ensure it's the right size
            if canvas.size != (W, H):
                canvas = canvas.resize((W, H), Image.LANCZOS)
        except Exception as e:
            print(f"Failed to load background {background_path}: {e}")
            canvas = Image.new("RGB", (W, H), (34, 34, 34))
    else:
        # Default solid color background
        canvas = Image.new("RGB", (W, H), (34, 34, 34))

    def to_rect(r: dict) -> Tuple[int, int, int, int]:
        x = int((r["leftPct"] / 100) * W)
        y = int((r["topPct"] / 100) * H)
        w = int((r["widthPct"] / 100) * W)
        h = int((r["heightPct"] / 100) * H)
        return x, y, w, h

    rects = [to_rect(r) for r in tpl.get("rects", [])]

    for i, p in enumerate(selected_paths):
        if i >= len(rects):
            break
        try:
            img = Image.open(p).convert("RGB")
        except Exception as e:
            return jsonify({"ok": False, "error": f"open {p}: {e}"}), 500
        x, y, w, h = rects[i]
        scale = min(w / img.width, h / img.height)  # Use min to fit inside rect (letterbox/pillarbox)
        nw, nh = int(img.width * scale), int(img.height * scale)
        resized = img.resize((nw, nh), Image.LANCZOS)
        dx = x + (w - nw) // 2  # Center horizontally
        dy = y + (h - nh) // 2  # Center vertically
        canvas.paste(resized, (dx, dy))

    if filt == "black_white":
        canvas = ImageOps.grayscale(canvas).convert("RGB")
    elif filt == "sepia":
        g = ImageOps.colorize(ImageOps.grayscale(canvas), black="#2e1f0f", white="#f4e1c1")
        canvas = g.convert("RGB")

    ts = datetime.utcnow().strftime("%Y/%m/%d/%H%M%S_%f")
    out_path = PHOTOS_DIR / f"A4_{ts}.jpg"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=95)
    out_path.write_bytes(buf.getvalue())
    return jsonify({"ok": True, "path": str(out_path), "url": f"/photo/A4_{ts}.jpg"})


@app.post("/print")
def do_print():
    data = request.get_json(force=True)
    path = data.get("path")
    printer = data.get("printer")
    if not path:
        return jsonify({"ok": False, "error": "missing path"}), 400
    args = ["lp"]
    if printer:
        args += ["-d", printer]
    args += ["-o", "media=A4.Borderless", "-o", "fit-to-page=false", path]
    proc = subprocess.run(args, capture_output=True)
    if proc.returncode != 0:
        return jsonify({"ok": False, "error": proc.stderr.decode("utf-8", "ignore")}), 500
    return jsonify({"ok": True})


@app.get("/templates/index.json")
def get_templates_index():
    return send_file(TEMPLATES_PATH)


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"➡️  Starting server on http://{host}:{port}")
    app.run(host=host, port=port, debug=True, threaded=True)


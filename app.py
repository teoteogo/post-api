"""
app.py — Post Generator API
----------------------------
POST /generate             genera PNG da template HTML (alias retrocompat n8n)
POST /generate/post        genera PNG da template HTML
POST /generate/carosello   genera ZIP (5 PNG + PDF) da template carosello
GET  /health               health check per Railway
GET  /routes               lista endpoint disponibili
"""

import base64
import io
import json
import logging
import os
import urllib.request
import uuid
import zipfile
from pathlib import Path

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

from flask import Flask, jsonify, request, send_file

app = Flask(__name__)

TEMPLATE_PATH            = Path(__file__).parent / "template.html"
TEMPLATE_CAROSELLO_PATH  = Path(__file__).parent / "template_carosello.html"

PLACEHOLDER_MAP = {
    "{{NOME_RUBRICA}}":   "rubrica",
    "{{TITLE}}":          "title",
    "{{DESCRIZIONE}}":    "descrizione",
    "{{BACKGROUND_URL}}": "bg_url",
    "{{POST_IMAGE_URL}}": "image_url",
    "{{LOGO_URL}}":       "logo_url",
    "{{COLOR_PRIMARY}}":  "color_primary",
}

CAROSELLO_FIELDS = [
    "TAG_1", "HEAD_1A", "HEAD_1B", "BODY_1",
    "TAG_2", "HEAD_2", "BODY_2",
    "TAG_3", "HEAD_3", "IMAGE_3",
    "TAG_4", "HEAD_4", "BODY_4",
    "TAG_5", "HEAD_5", "BODY_5", "CTA_5", "EMAIL_5",
    "LOGO_URL",
    "BG_GRAD_1", "BG_GRAD_2", "BG_GRAD_3", "BG_LIGHT", "BG_DARK",
]


# ── Render HTML ──────────────────────────────────────────────────────────────

def _render_html(data: dict) -> str:
    """Template post: sostituzione placeholder con valori JSON."""
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    for placeholder, field in PLACEHOLDER_MAP.items():
        html = html.replace(placeholder, data.get(field, ""))
    return html


def _fetch_image_as_data_uri(url: str) -> str:
    """Scarica un'immagine da URL e la restituisce come data URI base64."""
    if not url:
        return ""

    from PIL import Image

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = resp.read()

    pil_img = Image.open(io.BytesIO(data)).convert("RGB")
    max_side = 500
    w, h = pil_img.size
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        pil_img = pil_img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    jpeg_buf = io.BytesIO()
    pil_img.save(jpeg_buf, format="JPEG", quality=85)
    data = jpeg_buf.getvalue()

    b64 = base64.b64encode(data).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _render_carosello_html(data: dict) -> str:
    """Template carosello: sostituisce tutti i {{PLACEHOLDER}}.
    IMAGE_3 viene convertita da URL a data URI prima della sostituzione."""
    html = TEMPLATE_CAROSELLO_PATH.read_text(encoding="utf-8")
    image_url = data.get("IMAGE_3", "")
    image_data_uri = _fetch_image_as_data_uri(image_url) if image_url else ""
    substitutions = {**data, "IMAGE_3": image_data_uri}
    for field in CAROSELLO_FIELDS:
        html = html.replace("{{" + field + "}}", substitutions.get(field, ""))
    return html


# ── Playwright ───────────────────────────────────────────────────────────────

def _html_to_png(html: str) -> bytes:
    """Screenshot singolo: viewport 540x675, DSF 2 → PNG 1080x1350."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 540, "height": 675},
            device_scale_factor=2.0,
        )
        page.set_content(html, wait_until="networkidle")
        page.wait_for_timeout(3000)
        png = page.screenshot(clip={"x": 0, "y": 0, "width": 540, "height": 675})
        browser.close()

    return png


async def _carosello_to_pngs_async(html: str, total: int = 5, w: int = 320, h: int = 400) -> list:
    """Screenshot delle 5 slide del carosello via file temporaneo (evita timeout set_content)."""
    import tempfile
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            viewport={"width": w, "height": h},
            device_scale_factor=1080 / w,
        )

        with tempfile.NamedTemporaryFile(mode='w', suffix='.html',
                                         delete=False, encoding='utf-8') as f:
            f.write(html)
            tmp_path = f.name

        try:
            await page.goto(f"file://{tmp_path}",
                            wait_until="networkidle", timeout=60000)
            logger.error("Playwright: pagina caricata")
            await page.wait_for_timeout(3000)

            await page.evaluate("""() => {
                document.querySelectorAll('.nav-pills')
                    .forEach(el => el.style.display = 'none');
                const cv = document.querySelector('.cv');
                if (cv) { cv.style.boxShadow = 'none'; cv.style.borderRadius = '0'; }
            }""")

            pngs = []
            for i in range(total):
                logger.error(f"Inizio loop slide {i}")
                await page.evaluate(f"""() => {{
                    const ct = document.querySelector('.ct');
                    ct.style.transition = 'none';
                    ct.style.transform = 'translateX({-i * w}px)';
                }}""")
                await page.wait_for_timeout(300)
                png = await page.screenshot(clip={"x": 0, "y": 0, "width": w, "height": h})
                logger.error(f"Playwright: screenshot slide {i+1} completato")
                pngs.append(png)

            await browser.close()
            return pngs
        finally:
            os.unlink(tmp_path)


def _carosello_to_pngs(html: str) -> list:
    import asyncio
    import concurrent.futures

    def run_in_thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(_carosello_to_pngs_async(html))
        finally:
            loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_in_thread)
        return future.result(timeout=120)


def _pngs_to_pdf(pngs: list) -> bytes:
    """Assembla i PNG in un PDF: 1 pagina per slide, 810x1012.5 pt (= 1080x1350 px @ 96dpi)."""
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader

    W_PT = 1080 / 96 * 72   # 810.0 pt
    H_PT = 1350 / 96 * 72   # 1012.5 pt

    from PIL import Image

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W_PT, H_PT))
    for png_bytes in pngs:
        pil_img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        rgb_buf = io.BytesIO()
        pil_img.save(rgb_buf, format="PNG")
        rgb_buf.seek(0)
        img = ImageReader(rgb_buf)
        c.drawImage(img, 0, 0, width=W_PT, height=H_PT)
        c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return jsonify({"status": "running", "service": "post-api"})


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/routes")
def routes():
    return jsonify({
        "endpoints": [
            {"method": "POST", "path": "/generate",           "description": "Genera PNG post (alias retrocompat n8n)"},
            {"method": "POST", "path": "/generate/post",      "description": "Genera PNG post da template.html"},
            {"method": "POST", "path": "/generate/carosello", "description": "Genera ZIP con 5 PNG + PDF da template_carosello.html"},
            {"method": "GET",  "path": "/health",             "description": "Health check Railway"},
            {"method": "GET",  "path": "/routes",             "description": "Lista endpoint disponibili"},
        ]
    })


@app.route("/generate",      methods=["POST"])
@app.route("/generate/post", methods=["POST"])
def generate():
    data = request.get_json(force=True, silent=True) or {}

    required = ["rubrica", "title", "descrizione", "bg_url", "image_url", "logo_url", "color_primary"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"status": "error", "missing_fields": missing}), 400

    try:
        html     = _render_html(data)
        png      = _html_to_png(html)
        filename = f"post_{data['rubrica'].lower()}_{uuid.uuid4().hex[:8]}.png"
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    return send_file(
        io.BytesIO(png),
        mimetype="image/png",
        as_attachment=True,
        download_name=filename,
    )


@app.post("/generate/carosello")
def generate_carosello():
    data = request.get_json(force=True, silent=True) or {}
    logger.error(f"REQUEST DATA: {json.dumps(data, ensure_ascii=False)[:500]}")

    optional = {"IMAGE_3"}
    missing = [f for f in CAROSELLO_FIELDS if f not in optional and not data.get(f)]
    if missing:
        return jsonify({"status": "error", "missing_fields": missing}), 400

    try:
        html   = _render_carosello_html(data)
        slides = _carosello_to_pngs(html)
        pdf    = _pngs_to_pdf(slides)

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, png in enumerate(slides):
                zf.writestr(f"slide_{i + 1}.png", png)
            zf.writestr("carosello.pdf", pdf)
        zip_buf.seek(0)

        filename = f"carosello_{uuid.uuid4().hex[:8]}.zip"
        return send_file(
            zip_buf,
            mimetype="application/zip",
            as_attachment=True,
            download_name=filename,
        )
    except Exception as e:
        import traceback
        logger.error(f"ERRORE /generate/carosello: {traceback.format_exc()}")
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

"""
app.py — Post Generator API
----------------------------
POST /generate   genera PNG da template HTML e lo restituisce come file binario
GET  /health     health check per Railway
"""

import io
import os
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_file

app = Flask(__name__)

TEMPLATE_PATH = Path(__file__).parent / "template.html"

PLACEHOLDER_MAP = {
    "{{NOME_RUBRICA}}":   "rubrica",
    "{{TITLE}}":          "title",
    "{{DESCRIZIONE}}":    "descrizione",
    "{{BACKGROUND_URL}}": "bg_url",
    "{{POST_IMAGE_URL}}": "image_url",
    "{{LOGO_URL}}":       "logo_url",
    "{{COLOR_PRIMARY}}":  "color_primary",
}


# ── Generazione HTML ───────────────────────────────────────────────────────────

def _render_html(data: dict) -> str:
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    for placeholder, field in PLACEHOLDER_MAP.items():
        html = html.replace(placeholder, data.get(field, ""))
    return html


# ── Screenshot con Playwright ──────────────────────────────────────────────────

def _html_to_png(html: str) -> bytes:
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


# ── Endpoint ───────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return jsonify({"status": "running", "service": "post-api"})


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/generate")
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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

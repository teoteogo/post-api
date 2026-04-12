"""
app.py — Post Generator API
----------------------------
POST /generate   genera PNG da template HTML e lo carica su Google Drive
GET  /health     health check per Railway
"""

import io
import json
import os
import tempfile
import uuid
from pathlib import Path

from flask import Flask, jsonify, request

app = Flask(__name__)

TEMPLATE_PATH   = Path(__file__).parent / "template.html"
DRIVE_FOLDER_ID = "1pVTOoOtwM7yA1OmPTFB1sZaq-M_alPpp"

PLACEHOLDER_MAP = {
    "{{NOME_RUBRICA}}":   "rubrica",
    "{{TITLE}}":          "title",
    "{{DESCRIZIONE}}":    "descrizione",
    "{{BACKGROUND_URL}}": "bg_url",
    "{{POST_IMAGE_URL}}": "image_url",
    "{{LOGO_URL}}":       "logo_url",
    "{{COLOR_PRIMARY}}":  "color_primary",
}


# ── Credenziali Google da env ──────────────────────────────────────────────────

def _google_creds():
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not raw:
        raise RuntimeError("Variabile d'ambiente GOOGLE_CREDENTIALS_JSON non impostata")
    info = json.loads(raw)
    from google.oauth2 import service_account
    return service_account.Credentials.from_service_account_info(
        info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )


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


# ── Upload su Google Drive ─────────────────────────────────────────────────────

def _upload_to_drive(png_bytes: bytes, filename: str) -> str:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload

    service = build("drive", "v3", credentials=_google_creds())

    file_metadata = {
        "name": filename,
        "parents": [DRIVE_FOLDER_ID],
    }
    media = MediaIoBaseUpload(io.BytesIO(png_bytes), mimetype="image/png")
    uploaded = (
        service.files()
        .create(body=file_metadata, media_body=media, fields="id, webViewLink")
        .execute()
    )

    # Rendi il file leggibile pubblicamente
    service.permissions().create(
        fileId=uploaded["id"],
        body={"type": "anyone", "role": "reader"},
    ).execute()

    return uploaded.get("webViewLink", "")


# ── Endpoint ───────────────────────────────────────────────────────────────────

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
        url      = _upload_to_drive(png, filename)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    return jsonify({"status": "ok", "drive_url": url, "filename": filename})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

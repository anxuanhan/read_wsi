#!/usr/bin/env python3
"""FastAPI WSI thumbnail service.

Run:
  python main.py

Then open:
  http://127.0.0.1:8000
"""

from __future__ import annotations

import html
import json
import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from read_wsi import create_wsi_preview


APP_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = APP_DIR / "uploads"
OUTPUT_DIR = APP_DIR / "outputs"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTS = {
    ".svs",
    ".ndpi",
    ".tif",
    ".tiff",
    ".czi",
    ".mrxs",
    ".scn",
    ".vms",
    ".vmu",
    ".bif",
}

app = FastAPI(title="WSI Thumbnail Viewer")
app.mount("/outputs", StaticFiles(directory=str(OUTPUT_DIR)), name="outputs")


def _safe_filename(filename: str) -> str:
    name = Path(filename).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name or "uploaded_slide"


def _format_file_size(size_gb: Any) -> str:
    try:
        gb = float(size_gb)
    except (TypeError, ValueError):
        return "Unknown"
    if gb >= 1:
        return f"{gb:.2f} GB"
    return f"{gb * 1024:.0f} MB"


def _format_dimensions(metadata: dict[str, Any]) -> str:
    dims = metadata.get("dimensions")
    if not dims:
        level_dimensions = metadata.get("level_dimensions") or []
        dims = level_dimensions[0] if level_dimensions else None
    if not dims or len(dims) < 2:
        return "Unknown"
    return f"{int(dims[0])} × {int(dims[1])} px"


def _format_objective(metadata: dict[str, Any]) -> str:
    value = metadata.get("objective_power") or metadata.get("nominal_magnification")
    if not value:
        return "Unknown"
    text = str(value).strip()
    if text.lower().endswith("x"):
        return text
    try:
        number = float(text)
        return f"{number:g}×"
    except ValueError:
        return text


def _format_mpp(metadata: dict[str, Any]) -> str:
    value = metadata.get("mpp_x") or metadata.get("pixel_size_x_um")
    if value is None:
        return "Unknown"
    try:
        return f"{float(value):.3f} μm/pixel"
    except (TypeError, ValueError):
        return f"{value} μm/pixel"


def _format_format(metadata: dict[str, Any], filename: str) -> str:
    fmt = metadata.get("format")
    if fmt:
        return str(fmt).upper()
    suffix = Path(filename).suffix.lower().lstrip(".")
    return suffix.upper() if suffix else "Unknown"


def _metadata_rows(metadata: dict[str, Any], filename: str) -> list[tuple[str, str]]:
    return [
        ("Filename", filename),
        ("Format", _format_format(metadata, filename)),
        ("Dimensions", _format_dimensions(metadata)),
        ("Objective", _format_objective(metadata)),
        ("MPP", _format_mpp(metadata)),
        ("File Size", _format_file_size(metadata.get("source_size_gb"))),
    ]


def _metadata_table(metadata: dict[str, Any], filename: str) -> str:
    rows = []
    for label, value in _metadata_rows(metadata, filename):
        rows.append(
            f"""<div class="meta-row">
  <div class="meta-label">{html.escape(label)}</div>
  <div class="meta-value">{html.escape(value)}</div>
</div>"""
        )
    return "\n".join(rows)


def _page(content: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>WSI Thumbnail Viewer</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #eef0ed;
      color: #202427;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      padding: 32px;
      background:
        linear-gradient(180deg, #ffffff 0%, #f1f3ef 46%, #e7e9e5 100%);
    }}
    main {{
      max-width: 1200px;
      margin: 0 auto;
    }}
    .app-header {{
      display: flex;
      align-items: flex-end;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 18px;
    }}
    h1 {{
      margin: 0;
      font-size: 28px;
      line-height: 1.15;
      letter-spacing: 0;
    }}
    .eyebrow {{
      margin-bottom: 8px;
      color: #46606f;
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    .subtitle {{
      margin: 8px 0 0;
      color: #596268;
      font-size: 14px;
      line-height: 1.45;
    }}
    .status-pill {{
      flex: 0 0 auto;
      padding: 8px 11px;
      border: 1px solid #cdd5cb;
      border-radius: 999px;
      background: #f7faf6;
      color: #315846;
      font-size: 12px;
      font-weight: 800;
    }}
    form {{
      display: grid;
      grid-template-columns: 1fr 160px;
      gap: 14px;
      align-items: end;
      margin-bottom: 18px;
      padding: 18px;
      background: rgba(255, 255, 255, 0.94);
      border: 1px solid #d7ddd6;
      border-radius: 8px;
      box-shadow: 0 18px 42px rgba(37, 45, 52, 0.10);
    }}
    label {{
      display: grid;
      gap: 6px;
      font-size: 13px;
      color: #444;
    }}
    input, select, button {{
      box-sizing: border-box;
      width: 100%;
      min-height: 42px;
      font-size: 14px;
    }}
    input[type="file"] {{
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
    }}
    .file-button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 38px;
      padding: 0 16px;
      border: 1px solid #aeb8b0;
      border-radius: 6px;
      background: #f9fbf8;
      color: #202427;
      font-size: 14px;
      font-weight: 700;
      cursor: pointer;
    }}
    input[type="number"] {{
      padding: 0 10px;
      border: 1px solid #c4ccc4;
      border-radius: 6px;
      background: #fbfcfb;
      color: #202427;
      font-weight: 700;
    }}
    button {{
      border: 0;
      border-radius: 6px;
      background: #284f8f;
      color: white;
      font-weight: 700;
      cursor: pointer;
      box-shadow: 0 8px 18px rgba(40, 79, 143, 0.24);
    }}
    button:disabled {{
      cursor: wait;
      opacity: 0.72;
    }}
    .dropzone {{
      grid-column: 1 / -1;
      display: grid;
      gap: 12px;
      justify-items: center;
      padding: 34px 24px;
      border: 1.5px dashed #9fb0b8;
      border-radius: 8px;
      background: #f8faf8;
      text-align: center;
      color: #444;
      transition: border-color 120ms ease, background 120ms ease;
    }}
    .dropzone.dragover {{
      border-color: #284f8f;
      background: #eef4f8;
    }}
    .drop-title {{
      font-size: 18px;
      font-weight: 700;
      color: #202427;
    }}
    .drop-hint, .file-info, .progress-text {{
      font-size: 13px;
      color: #666;
    }}
    .file-info {{
      min-height: 18px;
      color: #315846;
      font-weight: 700;
    }}
    .progress-wrap {{
      grid-column: 1 / -1;
      display: none;
      gap: 8px;
    }}
    .progress-track {{
      width: 100%;
      height: 12px;
      overflow: hidden;
      border-radius: 999px;
      background: #dde4df;
    }}
    .progress-bar {{
      width: 0%;
      height: 100%;
      background: #2d7b65;
      transition: width 120ms linear;
    }}
    img {{
      display: block;
      max-width: 100%;
      height: auto;
      background: white;
      border: 1px solid #c6ccc6;
      border-radius: 8px;
    }}
    pre {{
      overflow: auto;
      padding: 14px;
      background: #fff;
      border: 1px solid #d8d8d2;
      border-radius: 8px;
      font-size: 12px;
      line-height: 1.45;
    }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin: 0;
    }}
    .meta-row {{
      padding: 14px;
      background: #fff;
      border: 1px solid #d7ddd6;
      border-radius: 8px;
    }}
    .meta-label {{
      margin-bottom: 5px;
      color: #666;
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
    }}
    .meta-value {{
      font-size: 16px;
      font-weight: 700;
      color: #202427;
      overflow-wrap: anywhere;
    }}
    .links {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin: 0;
    }}
    .links a {{
      display: inline-flex;
      align-items: center;
      min-height: 34px;
      padding: 0 10px;
      border: 1px solid #c8d2ca;
      border-radius: 6px;
      background: #fff;
      color: #284f8f;
      font-weight: 700;
      text-decoration: none;
    }}
    .intro-panel {{
      padding: 18px;
      border: 1px solid #d7ddd6;
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.72);
      color: #596268;
      font-size: 13px;
      line-height: 1.5;
    }}
    .result-grid {{
      display: grid;
      grid-template-columns: 360px minmax(0, 1fr);
      gap: 18px;
      align-items: start;
    }}
    .result-panel, .image-panel {{
      padding: 16px;
      border: 1px solid #d7ddd6;
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.94);
      box-shadow: 0 18px 42px rgba(37, 45, 52, 0.10);
    }}
    .result-panel {{
      display: grid;
      gap: 16px;
    }}
    .result-panel .summary {{
      grid-template-columns: 1fr;
    }}
    .image-panel {{
      min-width: 0;
    }}
    .image-title {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
      color: #596268;
      font-size: 13px;
      font-weight: 700;
    }}
    @media (max-width: 900px) {{
      form {{
        grid-template-columns: 1fr;
      }}
      .summary {{
        grid-template-columns: 1fr;
      }}
      .app-header {{
        display: grid;
      }}
      .result-grid {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
<main>
{content}
</main>
</body>
</html>"""
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return _page(
        """<div class="app-header">
  <div>
    <div class="eyebrow">Digital pathology</div>
    <h1>WSI Thumbnail Viewer</h1>
    <p class="subtitle">Upload a whole-slide image to generate a compact preview and readable slide summary.</p>
  </div>
  <div class="status-pill">Auto reader enabled</div>
</div>
<form id="upload-form" action="/upload" method="post" enctype="multipart/form-data">
  <div id="dropzone" class="dropzone">
    <div class="drop-title">Choose a WSI file or drag it here</div>
    <div class="drop-hint">Supported formats: SVS, NDPI, TIFF, CZI, MRXS, SCN, VMS, VMU, BIF</div>
    <input id="file-input" type="file" name="file" accept=".svs,.ndpi,.tif,.tiff,.czi,.mrxs,.scn,.vms,.vmu,.bif" required>
    <label class="file-button" for="file-input">Choose file</label>
    <div id="file-info" class="file-info">No file selected</div>
  </div>
  <label>Max side
    <input type="number" name="max_size" value="1200" min="128" max="8000" step="1">
  </label>
  <input type="hidden" name="reader" value="auto">
  <input type="hidden" name="white_balance" value="czi">
  <button type="submit">Upload</button>
  <div id="progress-wrap" class="progress-wrap">
    <div class="progress-track"><div id="progress-bar" class="progress-bar"></div></div>
    <div id="progress-text" class="progress-text">Waiting to upload...</div>
  </div>
</form>
<div class="intro-panel">Supported formats include SVS, NDPI, TIFF, CZI, MRXS, SCN, VMS, VMU, and BIF. The viewer automatically selects the appropriate reader and returns a 1200 px thumbnail by default.</div>
<script>
  const form = document.getElementById('upload-form');
  const dropzone = document.getElementById('dropzone');
  const fileInput = document.getElementById('file-input');
  const fileInfo = document.getElementById('file-info');
  const progressWrap = document.getElementById('progress-wrap');
  const progressBar = document.getElementById('progress-bar');
  const progressText = document.getElementById('progress-text');
  const uploadButton = form.querySelector('button[type="submit"]');

  function formatBytes(bytes) {
    if (!Number.isFinite(bytes)) return 'Unknown size';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let value = bytes;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    const digits = index === 0 ? 0 : value >= 100 ? 0 : value >= 10 ? 1 : 2;
    return `${value.toFixed(digits)} ${units[index]}`;
  }

  function updateFileInfo() {
    const file = fileInput.files[0];
    if (!file) {
      fileInfo.textContent = 'No file selected';
      return;
    }
    fileInfo.textContent = `${file.name} · ${formatBytes(file.size)}`;
  }

  fileInput.addEventListener('change', updateFileInfo);

  ['dragenter', 'dragover'].forEach((eventName) => {
    dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.add('dragover');
    });
  });

  ['dragleave', 'drop'].forEach((eventName) => {
    dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.remove('dragover');
    });
  });

  dropzone.addEventListener('drop', (event) => {
    const files = event.dataTransfer.files;
    if (!files.length) return;
    fileInput.files = files;
    updateFileInfo();
  });

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (!fileInput.files.length) {
      fileInfo.textContent = 'Please choose a WSI file first.';
      return;
    }

    const formData = new FormData(form);
    const xhr = new XMLHttpRequest();
    progressWrap.style.display = 'grid';
    progressBar.style.width = '0%';
    progressText.textContent = `Uploading ${fileInput.files[0].name} (${formatBytes(fileInput.files[0].size)})...`;
    uploadButton.disabled = true;

    xhr.upload.addEventListener('progress', (event) => {
      if (!event.lengthComputable) {
        progressText.textContent = 'Uploading...';
        return;
      }
      const percent = Math.round((event.loaded / event.total) * 100);
      progressBar.style.width = `${percent}%`;
      progressText.textContent = `Uploading ${formatBytes(event.loaded)} / ${formatBytes(event.total)} (${percent}%)`;
    });

    xhr.addEventListener('load', () => {
      uploadButton.disabled = false;
      if (xhr.status >= 200 && xhr.status < 300) {
        progressBar.style.width = '100%';
        progressText.textContent = 'Upload complete. Processing finished.';
        document.open();
        document.write(xhr.responseText);
        document.close();
      } else {
        progressText.textContent = `Upload failed: ${xhr.status} ${xhr.responseText}`;
      }
    });

    xhr.addEventListener('error', () => {
      uploadButton.disabled = false;
      progressText.textContent = 'Upload failed due to a network error.';
    });

    xhr.open('POST', form.action);
    xhr.send(formData);
  });
</script>"""
    )


async def _process_upload(
    file: UploadFile = File(...),
    max_size: int = Form(1200),
    reader: str = Form("auto"),
    white_balance: str = Form("czi"),
) -> dict[str, Any]:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail=f"Unsupported file extension: {suffix}")
    if reader not in {"auto", "openslide", "tifffile", "czi"}:
        raise HTTPException(status_code=400, detail=f"Unsupported reader: {reader}")
    if white_balance not in {"czi", "auto", "off"}:
        raise HTTPException(status_code=400, detail=f"Unsupported white_balance: {white_balance}")
    if max_size < 128 or max_size > 8000:
        raise HTTPException(status_code=400, detail="max_size must be between 128 and 8000")

    upload_id = uuid.uuid4().hex
    saved_name = f"{upload_id}_{_safe_filename(file.filename or 'uploaded_slide')}"
    slide_path = UPLOAD_DIR / saved_name

    try:
        with slide_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)

        job_output_dir = OUTPUT_DIR / upload_id
        result = create_wsi_preview(
            slide_path,
            job_output_dir,
            max_size=max_size,
            reader=reader,
            white_balance=white_balance,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        await file.close()

    return {"upload_id": upload_id, "slide_path": str(slide_path), "result": result}


@app.post("/upload", response_class=HTMLResponse)
async def upload_wsi(
    file: UploadFile = File(...),
    max_size: int = Form(1200),
    reader: str = Form("auto"),
    white_balance: str = Form("czi"),
) -> HTMLResponse:
    payload = await _process_upload(file, max_size=max_size, reader=reader, white_balance=white_balance)
    upload_id = payload["upload_id"]
    result = payload["result"]
    thumbnail_rel = f"/outputs/{upload_id}/{Path(result['thumbnail_path']).name}"
    metadata_rel = f"/outputs/{upload_id}/{Path(result['metadata_path']).name}"
    html_rel = f"/outputs/{upload_id}/{Path(result['html_path']).name}"
    metadata = result["metadata"]
    filename = file.filename or Path(payload["slide_path"]).name

    return _page(
        f"""<div class="app-header">
  <div>
    <div class="eyebrow">Preview ready</div>
    <h1>{html.escape(filename)}</h1>
  </div>
  <div class="status-pill">{html.escape(_format_format(metadata, filename))}</div>
</div>
<section class="result-grid">
  <aside class="result-panel">
    <div class="links">
      <a href="/">Upload another</a>
      <a href="{html.escape(thumbnail_rel)}" target="_blank">PNG</a>
      <a href="{html.escape(metadata_rel)}" target="_blank">Metadata JSON</a>
    </div>
    <section class="summary">
    {_metadata_table(metadata, filename)}
    </section>
  </aside>
  <section class="image-panel">
    <div class="image-title">
      <span>Thumbnail</span>
      <span>{html.escape(str(metadata.get("thumbnail_size_after_display_adjustment") or metadata.get("thumbnail_size") or ""))}</span>
    </div>
    <img src="{html.escape(thumbnail_rel)}" alt="WSI thumbnail">
  </section>
</section>
"""
    )


@app.post("/api/upload")
async def upload_wsi_api(
    file: UploadFile = File(...),
    max_size: int = Form(1200),
    reader: str = Form("auto"),
    white_balance: str = Form("czi"),
) -> JSONResponse:
    payload = await _process_upload(file, max_size=max_size, reader=reader, white_balance=white_balance)
    upload_id = payload["upload_id"]
    result = payload["result"]
    return JSONResponse(
        {
            "upload_id": upload_id,
            "thumbnail_url": f"/outputs/{upload_id}/{Path(result['thumbnail_path']).name}",
            "metadata_url": f"/outputs/{upload_id}/{Path(result['metadata_path']).name}",
            "html_url": f"/outputs/{upload_id}/{Path(result['html_path']).name}",
            "metadata": result["metadata"],
        }
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)

def main():
    print("Hello from pdf-to-md!")


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
server.py — Server HTTP per conversione PDF → Markdown tramite marker-pdf.
Ascolta sulla porta 9120 e accetta richieste dal client pdf2md.py.

Installazione dipendenze:
  pip install marker-pdf fastapi uvicorn python-multipart

Avvio:
  python server.py [--host 0.0.0.0] [--port 9120] [--workers 2]

Endpoint:
  POST /convert
    - Multipart form: file=<pdf_bytes>, filename=<nome.pdf>
    - Risposta JSON:
        {
          "status": "ok",
          "filename": "documento.pdf",
          "pages": 42,
          "markdown": "# documento\n\n...",
          "images": {
            "immagine-0.png": "<base64>",
            ...
          }
        }

  GET  /health   → {"status": "ok", "marker_version": "..."}
  GET  /info     → capacità del server (modelli disponibili, config)
"""

import base64
import logging
import os
import shutil
import tempfile
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pdf2md-server")

# ---------------------------------------------------------------------------
# App FastAPI
# ---------------------------------------------------------------------------
app = FastAPI(
    title="pdf2md-server",
    description="Conversione PDF → Markdown via marker-pdf",
    version="1.0.0",
)

# ---------------------------------------------------------------------------
# Lazy-import di marker per evitare errori di avvio se non installato
# ---------------------------------------------------------------------------
_marker_available = False
_marker_version   = "unknown"

def _load_marker():
    global _marker_available, _marker_version
    try:
        import marker
        _marker_version = getattr(marker, "__version__", "installed")
        _marker_available = True
        log.info(f"marker-pdf caricato — versione: {_marker_version}")
    except ImportError as e:
        log.error(f"marker-pdf NON disponibile: {e}")
        log.error("Installa con:  pip install marker-pdf")

_load_marker()


# ---------------------------------------------------------------------------
# Conversione reale con marker-pdf
# ---------------------------------------------------------------------------

def convert_pdf_with_marker(pdf_path: Path, work_dir: Path) -> dict:
    """
    Esegue la conversione tramite marker-pdf.
    Restituisce:
      {
        "markdown": str,
        "pages": int,
        "images": { "nome.png": "<base64>", ... }
      }
    """
    if not _marker_available:
        raise RuntimeError(
            "marker-pdf non è installato sul server. "
            "Eseguire: pip install marker-pdf"
        )

    from marker.convert import convert_single_pdf
    from marker.models import load_all_models

    log.info(f"Carico modelli marker...")
    model_lst = load_all_models()

    log.info(f"Converto: {pdf_path.name}")
    full_text, images, out_meta = convert_single_pdf(
        str(pdf_path),
        model_lst,
        max_pages=None,
        langs=None,
        batch_multiplier=1,
    )

    n_pages = out_meta.get("pages", 0) if isinstance(out_meta, dict) else 0

    # Salva immagini prodotte da marker e raccoglile come base64
    images_b64: dict[str, str] = {}
    if images:
        img_out = work_dir / "images"
        img_out.mkdir(exist_ok=True)
        for img_name, pil_img in images.items():
            img_path = img_out / img_name
            pil_img.save(str(img_path))
            with open(img_path, "rb") as f:
                images_b64[img_name] = base64.b64encode(f.read()).decode()

    return {
        "markdown": full_text,
        "pages":    n_pages,
        "images":   images_b64,
    }


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status":         "ok",
        "marker_version": _marker_version,
        "marker_ready":   _marker_available,
    }


@app.get("/info")
async def info():
    return {
        "server":   "pdf2md-server",
        "version":  "1.0.0",
        "backend":  "marker-pdf",
        "marker":   _marker_version,
        "ready":    _marker_available,
        "endpoints": [
            "POST /convert  — converte PDF in Markdown",
            "GET  /health   — stato del server",
            "GET  /info     — queste informazioni",
        ],
    }


@app.post("/convert")
async def convert(
    file: UploadFile = File(..., description="File PDF da convertire"),
):
    """
    Accetta un PDF in upload (multipart/form-data) e restituisce il Markdown.

    Il client invia:
        POST /convert
        Content-Type: multipart/form-data
        file=<bytes del PDF>

    Risposta JSON:
        {
          "status":   "ok",
          "filename": "nome.pdf",
          "pages":    42,
          "markdown": "# ...",
          "images":   { "img-0.png": "<base64>", ... }
        }
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="Il file deve essere un PDF (estensione .pdf).",
        )

    if not _marker_available:
        raise HTTPException(
            status_code=503,
            detail="marker-pdf non è disponibile sul server. Installarlo con: pip install marker-pdf",
        )

    # Scrive il PDF su disco in una cartella temporanea
    work_dir = Path(tempfile.mkdtemp(prefix="pdf2md_"))
    pdf_path = work_dir / file.filename

    try:
        content = await file.read()
        pdf_path.write_bytes(content)
        log.info(
            f"Ricevuto: {file.filename}  ({len(content) / 1024:.1f} KB)"
        )

        result = convert_pdf_with_marker(pdf_path, work_dir)

        log.info(
            f"Conversione completata: {file.filename}  "
            f"({result['pages']} pag, "
            f"{len(result['markdown'])} chars, "
            f"{len(result['images'])} immagini)"
        )

        return JSONResponse(
            content={
                "status":   "ok",
                "filename": file.filename,
                "pages":    result["pages"],
                "markdown": result["markdown"],
                "images":   result["images"],
            }
        )

    except Exception as e:
        log.error(f"Errore conversione {file.filename}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Errore durante la conversione: {e}",
        )

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Server PDF→Markdown via marker-pdf",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Esempi:\n"
            "  python server.py\n"
            "  python server.py --host 0.0.0.0 --port 9120\n"
            "  python server.py --workers 2\n"
        ),
    )
    parser.add_argument("--host",    default="0.0.0.0", help="Indirizzo bind (default: 0.0.0.0)")
    parser.add_argument("--port",    type=int, default=9120, help="Porta (default: 9120)")
    parser.add_argument("--workers", type=int, default=1,    help="Worker uvicorn (default: 1)")
    parser.add_argument("--reload",  action="store_true",    help="Auto-reload su modifica (dev)")
    args = parser.parse_args()

    log.info(f"Avvio server su http://{args.host}:{args.port}")
    log.info(f"marker-pdf: {'✓ disponibile' if _marker_available else '✗ NON disponibile'}")

    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        workers=args.workers,
        reload=args.reload,
        log_level="info",
    )
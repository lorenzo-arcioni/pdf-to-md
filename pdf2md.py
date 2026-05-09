#!/usr/bin/env python3
"""
pdf2md.py — Converte PDF in Markdown inviando il file a un server remoto
            che usa marker-pdf per la conversione.
Supporta resume: se interrotto, riparte dall'ultima conversione completata.

Il client invia l'intero PDF al server (POST /convert) e riceve il Markdown
e le eventuali immagini estratte come risposta JSON.

Struttura output:
  outputs/
    <nome_pdf>/
      <nome_pdf>.md          ← documento Markdown finale
      .progress.json         ← stato di avanzamento (resume)
      images/
        <img>.png            ← immagini restituite dal server

Uso:
  uv run pdf2md.py [--books-dir books] [--output-dir outputs]
                   [--server-url http://192.168.2.36:9120]
                   [--pdf path/to/file.pdf] [--force]
                   [--timeout 600]
"""

import argparse
import asyncio
import base64
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Costanti di default
# ---------------------------------------------------------------------------
DEFAULT_BOOKS_DIR  = "books"
DEFAULT_OUTPUT_DIR = "outputs"
DEFAULT_SERVER_URL = "http://192.168.2.36:9120"
DEFAULT_TIMEOUT    = 600   # secondi — marker-pdf può essere lento su PDF grandi

PROGRESS_FILE = ".progress.json"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str, level: str = "INFO") -> None:
    icons = {"INFO": "✓", "WARN": "⚠", "ERR": "✗", "STEP": "→"}
    print(f"  {icons.get(level, '·')}  {msg}", flush=True)


# ---------------------------------------------------------------------------
# Stato persistente (resume)
# ---------------------------------------------------------------------------

class ProgressState:
    """
    Persistenza dello stato su .progress.json.

    {
      "pdf": "books/foo.pdf",
      "done": false,
      "started_at": "...",
      "updated_at": "...",
      "server_url": "http://..."
    }
    """

    def __init__(self, state_path: Path):
        self.path = state_path
        self.data: dict = {}

    def load(self) -> bool:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
                return True
            except Exception:
                pass
        return False

    def init(self, pdf_path: Path, server_url: str) -> None:
        self.data = {
            "pdf":        str(pdf_path),
            "done":       False,
            "server_url": server_url,
            "started_at": _now(),
            "updated_at": _now(),
        }
        self._save()

    def _save(self) -> None:
        self.data["updated_at"] = _now()
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    @property
    def is_done(self) -> bool:
        return self.data.get("done", False)

    def mark_done(self) -> None:
        self.data["done"] = True
        self._save()

    def set(self, key: str, value) -> None:
        self.data[key] = value
        self._save()

    def get(self, key: str, default=None):
        return self.data.get(key, default)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Verifica salute del server
# ---------------------------------------------------------------------------

async def check_server_health(server_url: str, timeout: float = 10.0) -> dict:
    """Controlla che il server sia raggiungibile e marker-pdf disponibile."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(f"{server_url}/health")
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Chiamata al server di conversione
# ---------------------------------------------------------------------------

async def convert_pdf_remote(
    pdf_path: Path,
    server_url: str,
    timeout: float,
) -> dict:
    """
    Invia il PDF al server e restituisce il risultato JSON:
    {
      "status":   "ok",
      "filename": "...",
      "pages":    N,
      "markdown": "...",
      "images":   { "nome.png": "<base64>", ... }
    }
    """
    pdf_bytes = pdf_path.read_bytes()
    size_kb   = len(pdf_bytes) / 1024

    log(f"Invio {pdf_path.name} ({size_kb:.1f} KB) → {server_url}/convert")

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{server_url}/convert",
            files={"file": (pdf_path.name, pdf_bytes, "application/pdf")},
        )
        resp.raise_for_status()

    return resp.json()


# ---------------------------------------------------------------------------
# Salvataggio output
# ---------------------------------------------------------------------------

def save_output(
    result: dict,
    doc_dir: Path,
    images_dir: Path,
    md_path: Path,
) -> list[Path]:
    """
    Scrive il Markdown su disco e salva le immagini ricevute dal server.
    Restituisce la lista dei file immagine salvati.
    """
    # Markdown
    md_path.write_text(result["markdown"], encoding="utf-8")

    # Immagini (base64 → file .png)
    saved_images: list[Path] = []
    images_b64 = result.get("images", {})
    if images_b64:
        images_dir.mkdir(parents=True, exist_ok=True)
        for img_name, b64_data in images_b64.items():
            img_path = images_dir / img_name
            img_path.write_bytes(base64.b64decode(b64_data))
            saved_images.append(img_path)
        log(f"Immagini salvate: {len(saved_images)}")

    return saved_images


# ---------------------------------------------------------------------------
# Formattazione tempo
# ---------------------------------------------------------------------------

def fmt_duration(seconds: float) -> str:
    if seconds < 0 or seconds > 86400 * 2:
        return "?"
    return str(timedelta(seconds=int(seconds)))


# ---------------------------------------------------------------------------
# Elaborazione singolo PDF
# ---------------------------------------------------------------------------

async def process_pdf(
    pdf_path: Path,
    output_dir: Path,
    server_url: str,
    timeout: float,
    force: bool = False,
) -> None:
    stem       = pdf_path.stem
    doc_dir    = output_dir / stem
    images_dir = doc_dir / "images"
    md_path    = doc_dir / f"{stem}.md"

    doc_dir.mkdir(parents=True, exist_ok=True)

    # ── Carica o inizializza stato ──────────────────────────────────────────
    state   = ProgressState(doc_dir / PROGRESS_FILE)
    resumed = state.load() and not force

    if resumed and state.is_done:
        print(f"\n  ✅  {pdf_path.name} già completato (usa --force per rielaborare).")
        return

    if resumed:
        print(f"\n{'='*62}")
        print(f"  ▶  RESUME: {pdf_path.name}")
        print(f"     Conversione precedente non completata — riprovo...")
        print(f"{'='*62}")
    else:
        if force:
            # Reset: rimuove MD e immagini precedenti
            import shutil
            if md_path.exists():
                md_path.unlink()
            if images_dir.exists():
                shutil.rmtree(images_dir)
            if (doc_dir / PROGRESS_FILE).exists():
                (doc_dir / PROGRESS_FILE).unlink()

        state.init(pdf_path, server_url)
        print(f"\n{'='*62}")
        print(f"  📄  {pdf_path.name}")
        print(f"{'='*62}")

    # ── Verifica server ─────────────────────────────────────────────────────
    log(f"Verifica server {server_url}...")
    try:
        health = await check_server_health(server_url)
        marker_ver = health.get("marker_version", "?")
        marker_ok  = health.get("marker_ready", False)
        status_icon = "✓" if marker_ok else "⚠"
        log(f"Server OK — marker-pdf {marker_ver} {status_icon}")
        if not marker_ok:
            log("marker-pdf non disponibile sul server!", "ERR")
            raise RuntimeError("marker-pdf non pronto sul server.")
    except httpx.ConnectError as e:
        log(f"Impossibile connettersi a {server_url}: {e}", "ERR")
        raise
    except httpx.HTTPStatusError as e:
        log(f"Server ha risposto con errore: {e.response.status_code}", "ERR")
        raise

    # ── Invio e conversione ─────────────────────────────────────────────────
    t0 = time.time()
    print(f"\n  → Conversione in corso (timeout: {timeout}s)...\n")

    try:
        result = await convert_pdf_remote(pdf_path, server_url, timeout)
    except asyncio.CancelledError:
        log("Conversione interrotta dall'utente.", "WARN")
        print(f"\n  💡  Per riprendere:  uv run pdf2md.py --pdf \"{pdf_path}\"\n")
        raise
    except httpx.ReadTimeout:
        elapsed = time.time() - t0
        log(
            f"Timeout dopo {elapsed:.0f}s. "
            f"Prova ad aumentare --timeout (attuale: {timeout}s).",
            "ERR",
        )
        raise
    except httpx.HTTPStatusError as e:
        body = e.response.text[:300]
        log(f"Errore HTTP {e.response.status_code}: {body}", "ERR")
        raise

    elapsed = time.time() - t0

    # ── Salva output ────────────────────────────────────────────────────────
    saved_images = save_output(result, doc_dir, images_dir, md_path)
    state.mark_done()

    # ── Summary ─────────────────────────────────────────────────────────────
    pages    = result.get("pages", "?")
    md_chars = len(result.get("markdown", ""))

    print(f"\n  {'─'*58}")
    print(f"  ✅  Completato: {stem}")
    print(f"      Pagine     : {pages}")
    print(f"      Markdown   : {md_chars:,} caratteri")
    if saved_images:
        print(f"      Immagini   : {len(saved_images)}")
    print(f"      Tempo      : {fmt_duration(elapsed)}")
    print(f"      Output     : {md_path}")
    print(f"  {'─'*58}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="PDF → Markdown via server remoto marker-pdf. Supporta resume.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Esempi:\n"
            "  uv run pdf2md.py                              # tutti i PDF in books/\n"
            "  uv run pdf2md.py --pdf books/mio.pdf          # solo un file\n"
            "  uv run pdf2md.py --force                      # rielabora da capo\n"
            "  uv run pdf2md.py --timeout 1200               # timeout più lungo\n"
            "  uv run pdf2md.py --server-url http://10.0.0.5:9120\n"
        ),
    )
    parser.add_argument(
        "--books-dir",  default=DEFAULT_BOOKS_DIR,
        metavar="DIR",  help="Cartella PDF sorgenti (default: books)",
    )
    parser.add_argument(
        "--output-dir", default=DEFAULT_OUTPUT_DIR,
        metavar="DIR",  help="Cartella output (default: outputs)",
    )
    parser.add_argument(
        "--server-url", default=DEFAULT_SERVER_URL,
        metavar="URL",  help=f"URL server marker-pdf (default: {DEFAULT_SERVER_URL})",
    )
    parser.add_argument(
        "--timeout",    type=float, default=DEFAULT_TIMEOUT,
        metavar="SEC",  help=f"Timeout richiesta in secondi (default: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--pdf",        default=None,
        metavar="FILE", help="Elabora solo questo PDF",
    )
    parser.add_argument(
        "--force",      action="store_true",
        help="Ignora stato salvato, rielabora da capo",
    )
    args = parser.parse_args()

    books_dir  = Path(args.books_dir)
    output_dir = Path(args.output_dir)

    if not books_dir.exists() and not args.pdf:
        print(f"\n  ✗  Cartella '{books_dir}' non trovata.")
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    pdfs = [Path(args.pdf)] if args.pdf else sorted(books_dir.glob("*.pdf"))

    if not pdfs:
        print(f"  Nessun PDF trovato in '{books_dir}'.")
        sys.exit(0)

    print(f"\n  Server  : {args.server_url}")
    print(f"  Timeout : {args.timeout}s")
    print(f"\n  PDF in coda: {len(pdfs)}")
    for p in pdfs:
        print(f"    • {p.name}")

    total_t0 = time.time()
    errors: list[tuple[str, str]] = []

    async def run_all() -> None:
        for pdf in pdfs:
            try:
                await process_pdf(
                    pdf_path   = pdf,
                    output_dir = output_dir,
                    server_url = args.server_url,
                    timeout    = args.timeout,
                    force      = args.force,
                )
            except asyncio.CancelledError:
                raise  # interrompe tutti i PDF rimanenti
            except Exception as e:
                log(f"Errore su {pdf.name}: {e}", "ERR")
                errors.append((pdf.name, str(e)))

    try:
        asyncio.run(run_all())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    elapsed = time.time() - total_t0
    print(f"  ⏱   Tempo totale: {fmt_duration(elapsed)}")
    if errors:
        print("\n  Errori:")
        for name, err in errors:
            print(f"    ✗ {name}: {err}")


if __name__ == "__main__":
    main()
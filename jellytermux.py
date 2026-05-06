#!/usr/bin/env python3
"""Mini Jellyfin-like media server for Termux.

Features:
- scans one or more media folders
- dashboard con navigazione per cartelle (click per entrare)
- simple web UI for browsing and playing files
- HTTP range requests for seeking in media players
- JSON API endpoint to retrieve indexed library

This is intentionally lightweight and uses only Python's standard library.
"""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import re
import secrets
import sys
from collections import defaultdict
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Iterable, List, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse

SUPPORTED_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".webm",
    ".mp3",
    ".flac",
    ".aac",
    ".m4a",
    ".wav",
    ".ogg",
}

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm"}


def human_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024.0
    return f"{num_bytes} B"


@dataclass(frozen=True)
class MediaItem:
    item_id: str
    path: Path
    rel_path: str          # relativo alla root di scansione
    folder: str            # cartella padre (prima componente di rel_path)
    size: int
    media_type: str


class MediaLibrary:
    def __init__(self, roots: Iterable[Path]) -> None:
        self.roots: List[Path] = [root.expanduser().resolve() for root in roots]
        self._items: Dict[str, MediaItem] = {}

    def scan(self) -> None:
        items: Dict[str, MediaItem] = {}
        for root in self.roots:
            if not root.exists() or not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue

                try:
                    rel = path.relative_to(root)
                except ValueError:
                    rel = Path(path.name)

                rel_path = str(rel)
                # La "cartella" è la prima directory sotto la root (o "/" se il file è in root)
                parts = rel.parts
                folder = parts[0] if len(parts) > 1 else "/"

                item_id = secrets.token_hex(8)
                media_type = "video" if path.suffix.lower() in VIDEO_EXTENSIONS else "audio"
                items[item_id] = MediaItem(
                    item_id=item_id,
                    path=path,
                    rel_path=rel_path,
                    folder=folder,
                    size=path.stat().st_size,
                    media_type=media_type,
                )

        self._items = items

    @property
    def items(self) -> Dict[str, MediaItem]:
        return self._items

    def folders(self) -> Dict[str, List[MediaItem]]:
        """Raggruppa gli item per cartella padre."""
        groups: Dict[str, List[MediaItem]] = defaultdict(list)
        for item in self._items.values():
            groups[item.folder].append(item)
        return dict(groups)


# ── HTML helpers ──────────────────────────────────────────────────────────────

_BASE_STYLE = """
body { font-family: system-ui, sans-serif; margin: 0; padding: 1rem;
       background: #121212; color: #f4f4f4; }
header { display: flex; gap: 1rem; align-items: center; flex-wrap: wrap; margin-bottom: .75rem; }
h1 { margin: 0; }
input, button { padding: .5rem; border-radius: .5rem; border: 1px solid #444;
                background: #1d1d1d; color: #f4f4f4; font-size: 1rem; }
button { cursor: pointer; }
button:hover { background: #2a2a2a; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
        gap: 1rem; margin-top: .5rem; }
.card { border: 1px solid #333; border-radius: .75rem; padding: .75rem;
        background: #181818; transition: border-color .2s; }
.card:hover { border-color: #5db0ff; }
.card h3 { margin: 0 0 .4rem; font-size: 1rem; word-break: break-word; }
.card p { margin: .2rem 0 .4rem; color: #aaa; font-size: .85rem; }
.folder-card { cursor: pointer; text-decoration: none; color: inherit; display: block; }
.folder-icon { font-size: 2.5rem; margin-bottom: .4rem; }
.item-count { font-size: .8rem; color: #888; }
video, audio { width: 100%; margin-top: .5rem; border-radius: .4rem; }
a.stream-link { color: #5db0ff; font-size: .85rem; }
.breadcrumb { font-size: .9rem; color: #aaa; margin-bottom: .5rem; }
.breadcrumb a { color: #5db0ff; text-decoration: none; }
.badge { display: inline-block; padding: .1rem .45rem; border-radius: .3rem;
         font-size: .75rem; background: #2d2d2d; color: #ccc; margin-left: .3rem; }
.badge.video { background: #1a3a5c; color: #7ec8ff; }
.badge.audio { background: #2a1a3a; color: #c07eff; }
"""


def _page(title: str, header_html: str, content_html: str) -> str:
    return f"""<!doctype html>
<html lang='it'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>{html.escape(title)}</title>
  <style>{_BASE_STYLE}</style>
</head>
<body>
  <header>
    {header_html}
  </header>
  {content_html}
</body>
</html>"""


# ── Handler ───────────────────────────────────────────────────────────────────

class JellyHandler(BaseHTTPRequestHandler):
    server: "JellyServer"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stdout.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            return self._handle_index(parsed.query)
        if path == "/browse":
            return self._handle_browse(parsed.query)
        if path == "/api/library":
            return self._handle_api_library()
        if path == "/rescan":
            return self._handle_rescan()
        if path.startswith("/stream/"):
            item_id = unquote(path[len("/stream/"):])
            return self._handle_stream(item_id)

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    # ── Dashboard: griglia di cartelle ────────────────────────────────────────

    def _handle_index(self, query: str) -> None:
        params = parse_qs(query)
        q = params.get("q", [""])[0].strip().lower()

        library = self.server.library

        # Se c'è una ricerca mostriamo i singoli file come prima
        if q:
            items = [item for item in library.items.values() if q in item.rel_path.lower()]
            return self._send_html(self._render_search_results(items, q))

        # Altrimenti mostriamo le cartelle
        folders = library.folders()

        cards = []
        for folder_name in sorted(folders.keys(), key=str.lower):
            folder_items = folders[folder_name]
            count = len(folder_items)
            video_count = sum(1 for i in folder_items if i.media_type == "video")
            audio_count = count - video_count
            browse_url = f"/browse?folder={quote(folder_name)}"
            display = html.escape(folder_name)

            sub = []
            if video_count:
                sub.append(f"<span class='badge video'>▶ {video_count} video</span>")
            if audio_count:
                sub.append(f"<span class='badge audio'>♪ {audio_count} audio</span>")
            sub_html = " ".join(sub)

            cards.append(
                f"<a class='card folder-card' href='{browse_url}'>"
                f"<div class='folder-icon'>📁</div>"
                f"<h3>{display}</h3>"
                f"<p class='item-count'>{count} file {sub_html}</p>"
                f"</a>"
            )

        total = len(library.items)
        content = (
            f"<p style='color:#aaa;'>{len(folders)} cartell{'a' if len(folders)==1 else 'e'} • {total} file totali</p>"
            f"<section class='grid'>{''.join(cards) if cards else '<p>Nessun file trovato.</p>'}</section>"
        )

        header = self._common_header(q)
        self._send_html(_page("MiniJellyTermux", header, content))

    # ── Pagina cartella ───────────────────────────────────────────────────────

    def _handle_browse(self, query: str) -> None:
        params = parse_qs(query)
        folder_name = unquote(params.get("folder", [""])[0])

        if not folder_name:
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/")
            self.end_headers()
            return

        library = self.server.library
        folders = library.folders()
        folder_items = folders.get(folder_name, [])

        rows = []
        for item in sorted(folder_items, key=lambda i: i.rel_path.lower()):
            # mostra il percorso relativo dentro la cartella (escludi il primo componente)
            parts = Path(item.rel_path).parts
            display_name = str(Path(*parts[1:])) if len(parts) > 1 else parts[0]
            escaped = html.escape(display_name)
            stream_url = f"/stream/{quote(item.item_id)}"
            player = (
                f'<video controls preload="metadata" src="{stream_url}"></video>'
                if item.media_type == "video"
                else f'<audio controls preload="metadata" src="{stream_url}"></audio>'
            )
            badge = f"<span class='badge {item.media_type}'>"
            badge += ("▶ video" if item.media_type == "video" else "♪ audio") + "</span>"

            rows.append(
                f"<div class='card'>"
                f"<h3>{escaped} {badge}</h3>"
                f"<p>{human_size(item.size)}</p>"
                f"<a class='stream-link' href='{stream_url}'>⬇ Stream diretto</a>"
                f"<div>{player}</div>"
                f"</div>"
            )

        breadcrumb = (
            f"<p class='breadcrumb'><a href='/'>🏠 Home</a> › "
            f"📁 {html.escape(folder_name)}</p>"
        )
        content = (
            breadcrumb
            + f"<p style='color:#aaa;'>{len(folder_items)} file</p>"
            + f"<section class='grid'>{''.join(rows) if rows else '<p>Nessun file in questa cartella.</p>'}</section>"
        )

        header = self._common_header("")
        self._send_html(_page(f"📁 {folder_name} – MiniJellyTermux", header, content))

    # ── Risultati ricerca ─────────────────────────────────────────────────────

    def _render_search_results(self, items: List[MediaItem], q: str) -> str:
        rows = []
        for item in sorted(items, key=lambda i: i.rel_path.lower()):
            escaped = html.escape(item.rel_path)
            stream_url = f"/stream/{quote(item.item_id)}"
            player = (
                f'<video controls preload="metadata" src="{stream_url}"></video>'
                if item.media_type == "video"
                else f'<audio controls preload="metadata" src="{stream_url}"></audio>'
            )
            badge = (
                f"<span class='badge {item.media_type}'>"
                + ("▶ video" if item.media_type == "video" else "♪ audio")
                + "</span>"
            )
            rows.append(
                f"<div class='card'>"
                f"<h3>{escaped} {badge}</h3>"
                f"<p>{human_size(item.size)}</p>"
                f"<a class='stream-link' href='{stream_url}'>⬇ Stream diretto</a>"
                f"<div>{player}</div>"
                f"</div>"
            )

        content = (
            f"<p style='color:#aaa;'>Risultati per '<b>{html.escape(q)}</b>': {len(items)} file</p>"
            f"<section class='grid'>{''.join(rows) if rows else '<p>Nessun risultato.</p>'}</section>"
        )
        return _page(f'Ricerca: {q}', self._common_header(q), content)

    # ── API ───────────────────────────────────────────────────────────────────

    def _handle_api_library(self) -> None:
        payload = [
            {
                "id": item.item_id,
                "name": item.rel_path,
                "folder": item.folder,
                "size": item.size,
                "stream": f"/stream/{quote(item.item_id)}",
                "media_type": item.media_type,
            }
            for item in sorted(self.server.library.items.values(), key=lambda i: i.rel_path.lower())
        ]
        self._send_json(payload)

    def _handle_rescan(self) -> None:
        self.server.library.scan()
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", "/")
        self.end_headers()

    # ── Stream con range requests ─────────────────────────────────────────────

    def _handle_stream(self, item_id: str) -> None:
        item = self.server.library.items.get(item_id)
        if item is None or not item.path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "File non trovato")
            return

        ctype, _ = mimetypes.guess_type(item.path.name)
        content_type = ctype or "application/octet-stream"
        file_size = item.path.stat().st_size

        range_header = self.headers.get("Range")
        if range_header:
            match = re.match(r"bytes=(\d*)-(\d*)", range_header)
            if not match:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return

            start_s, end_s = match.groups()
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else file_size - 1

            if start >= file_size or end >= file_size or start > end:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return

            length = end - start + 1
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            with item.path.open("rb") as fh:
                fh.seek(start)
                self.wfile.write(fh.read(length))
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(file_size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with item.path.open("rb") as fh:
            self.wfile.write(fh.read())

    # ── Utilità ───────────────────────────────────────────────────────────────

    def _common_header(self, q: str) -> str:
        return (
            "<h1 style='margin:0;'>🎬 MiniJellyTermux</h1>"
            "<form method='GET' action='/'>"
            f"  <input name='q' placeholder='Cerca file...' value='{html.escape(q)}'>"
            "  <button type='submit'>🔍</button>"
            "</form>"
            "<a href='/rescan'><button type='button'>🔄 Rescan</button></a>"
        )

    def _send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


# ── Server ────────────────────────────────────────────────────────────────────

class JellyServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: Tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        library: MediaLibrary,
    ):
        super().__init__(server_address, handler)
        self.library = library


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mini Jellyfin-like server per Termux")
    parser.add_argument(
        "media_dirs",
        nargs="+",
        help="Una o più cartelle da indicizzare (es: ~/storage/shared/Movies)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Host di bind (default: 0.0.0.0)")
    parser.add_argument("--port", default=8096, type=int, help="Porta (default: 8096)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    roots = [Path(p) for p in args.media_dirs]
    library = MediaLibrary(roots)
    library.scan()

    server = JellyServer((args.host, args.port), JellyHandler, library)
    host, port = server.server_address
    print(f"MiniJellyTermux in esecuzione su http://{host}:{port}")
    print(f"File indicizzati: {len(library.items)}")
    folders = library.folders()
    print(f"Cartelle trovate: {len(folders)}")
    for name, items in sorted(folders.items()):
        print(f"  📁 {name}  ({len(items)} file)")
    print("Premi CTRL+C per fermare il server.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArresto server in corso...")
    finally:
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

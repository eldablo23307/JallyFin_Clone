#!/usr/bin/env python3
"""Mini Jellyfin-like media server for Termux.

Features:
- scans one or more media folders
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
    rel_path: str
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
                    rel_path = str(path.relative_to(root))
                except ValueError:
                    rel_path = path.name

                item_id = secrets.token_hex(8)
                media_type = "video" if path.suffix.lower() in {".mp4", ".mkv", ".avi", ".mov", ".webm"} else "audio"
                items[item_id] = MediaItem(
                    item_id=item_id,
                    path=path,
                    rel_path=rel_path,
                    size=path.stat().st_size,
                    media_type=media_type,
                )

        self._items = items

    @property
    def items(self) -> Dict[str, MediaItem]:
        return self._items


class JellyHandler(BaseHTTPRequestHandler):
    server: "JellyServer"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stdout.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            return self._handle_index(parsed.query)
        if path == "/api/library":
            return self._handle_api_library()
        if path == "/rescan":
            return self._handle_rescan()
        if path.startswith("/stream/"):
            item_id = unquote(path[len("/stream/") :])
            return self._handle_stream(item_id)

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def _handle_index(self, query: str) -> None:
        params = parse_qs(query)
        q = params.get("q", [""])[0].strip().lower()

        items = list(self.server.library.items.values())
        if q:
            items = [item for item in items if q in item.rel_path.lower()]

        rows = []
        for item in sorted(items, key=lambda i: i.rel_path.lower()):
            escaped = html.escape(item.rel_path)
            stream_url = f"/stream/{quote(item.item_id)}"
            player = (
                f'<video controls preload="metadata" src="{stream_url}"></video>'
                if item.media_type == "video"
                else f'<audio controls preload="metadata" src="{stream_url}"></audio>'
            )
            rows.append(
                "<div class='item'>"
                f"<h3>{escaped}</h3>"
                f"<p>{human_size(item.size)} • {item.media_type}</p>"
                f"<a href='{stream_url}'>Apri stream diretto</a>"
                f"<div class='player'>{player}</div>"
                "</div>"
            )

        body = f"""<!doctype html>
<html lang='it'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>MiniJellyTermux</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 0; padding: 1rem; background: #121212; color: #f4f4f4; }}
    header {{ display: flex; gap: 1rem; align-items: center; flex-wrap: wrap; }}
    input, button {{ padding: .5rem; border-radius: .5rem; border: 1px solid #444; background: #1d1d1d; color: #f4f4f4; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 1rem; margin-top: 1rem; }}
    .item {{ border: 1px solid #333; border-radius: .75rem; padding: .75rem; background: #181818; }}
    video, audio {{ width: 100%; margin-top: .5rem; }}
    a {{ color: #5db0ff; }}
    p {{ margin: .25rem 0 .5rem; color: #ddd; }}
  </style>
</head>
<body>
  <header>
    <h1 style='margin:0;'>MiniJellyTermux</h1>
    <form method='GET' action='/'>
      <input name='q' placeholder='Cerca file...' value='{html.escape(q)}'>
      <button type='submit'>Cerca</button>
    </form>
    <a href='/rescan'><button type='button'>Rescan libreria</button></a>
  </header>
  <p>Elementi trovati: {len(items)}</p>
  <section class='grid'>
    {''.join(rows) if rows else '<p>Nessun file multimediale trovato.</p>'}
  </section>
</body>
</html>"""

        self._send_html(body)

    def _handle_api_library(self) -> None:
        payload = [
            {
                "id": item.item_id,
                "name": item.rel_path,
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


class JellyServer(ThreadingHTTPServer):
    def __init__(self, server_address: Tuple[str, int], handler: type[BaseHTTPRequestHandler], library: MediaLibrary):
        super().__init__(server_address, handler)
        self.library = library


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

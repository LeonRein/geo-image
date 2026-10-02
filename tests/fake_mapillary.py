"""A tiny in-process stand-in for graph.mapillary.com and its image CDN."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = "MLY|test"


def make_image(image_id, lon, lat, captured_at=1_700_000_000_000, camera_type="perspective", computed=True):
    img = {
        "id": str(image_id),
        "captured_at": captured_at,
        "compass_angle": 90.0,
        "computed_compass_angle": 91.5,
        "geometry": {"type": "Point", "coordinates": [lon + 0.00001, lat + 0.00001]},
        "altitude": 300.0,
        "computed_altitude": 301.0,
        "camera_type": camera_type,
        "make": "Pixel",
        "model": "7",
        "width": 4000,
        "height": 3000,
        "sequence": "seq1",
        "creator": {"username": "alice", "id": "1"},
    }
    if computed:
        img["computed_geometry"] = {"type": "Point", "coordinates": [lon, lat]}
    return img


class FakeMapillary:
    def __init__(self, images, fail_wider_than=None, slow_wider_than=None, expired_ids=()):
        self.images = {img["id"]: img for img in images}
        self.fail_wider_than = fail_wider_than
        self.slow_wider_than = slow_wider_than
        self.expired_ids = set(expired_ids)
        self.search_calls = []
        self.downloads = []
        self.refreshes = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                url = urlparse(self.path)
                qs = {k: v[0] for k, v in parse_qs(url.query).items()}
                parts = url.path.strip("/").split("/")
                if parts[0] == "cdn":
                    return fake._serve_image(self, parts[1], qs)
                if self.headers.get("Authorization") != f"OAuth {TOKEN}":
                    return self._json(401, {"error": {"message": "Invalid OAuth access token"}})
                if parts == ["images"]:
                    return fake._search(self, qs)
                return fake._get_image(self, parts[0], qs)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def _url(self, image_id, size_field, fresh=False):
        tag = "fresh" if fresh or image_id not in self.expired_ids else "expired"
        return f"{self.base_url}/cdn/{image_id}?size={size_field}&sig={tag}"

    def _project(self, img, fields):
        out = {k: img[k] for k in fields if k in img}
        for f in fields:
            if f.startswith("thumb_"):
                out[f] = self._url(img["id"], f)
        return out

    def _search(self, handler, qs):
        w, s, e, n = map(float, qs["bbox"].split(","))
        self.search_calls.append((w, s, e, n))
        if self.fail_wider_than and (e - w) > self.fail_wider_than:
            return handler._json(500, {"error": {"message": "Please reduce the amount of data"}})
        if self.slow_wider_than and (e - w) > self.slow_wider_than:
            time.sleep(0.5)  # longer than the client timeout used in the tests
        fields = qs["fields"].split(",")
        limit = int(qs.get("limit", 2000))
        hits = []
        for img in self.images.values():
            lon, lat = (img.get("computed_geometry") or img["geometry"])["coordinates"]
            if w <= lon <= e and s <= lat <= n:
                hits.append(self._project(img, fields))
        return handler._json(200, {"data": hits[:limit]})

    def _get_image(self, handler, image_id, qs):
        img = self.images.get(image_id)
        if not img:
            return handler._json(404, {"error": {"message": "not found"}})
        self.refreshes.append(image_id)
        fields = qs["fields"].split(",")
        out = {k: img[k] for k in fields if k in img}
        for f in fields:
            if f.startswith("thumb_"):
                out[f] = self._url(image_id, f, fresh=True)
        return handler._json(200, out)

    def _serve_image(self, handler, image_id, qs):
        if handler.headers.get("Authorization"):
            # The token must never be sent to the image CDN.
            handler.send_response(400)
            handler.end_headers()
            return
        if qs.get("sig") == "expired":
            handler.send_response(403)
            handler.end_headers()
            return
        self.downloads.append(image_id)
        data = f"JPEG:{image_id}:{qs.get('size')}".encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "image/jpeg")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

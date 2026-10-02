"""Download all Mapillary images inside a bounding box.

A job runs in three phases:

1. **Search** - the area is cut into cells of at most ``INITIAL_CELL_DEG``
   degrees. Each cell is queried with ``GET /images?bbox=...``. Because the
   API returns at most 2000 images per request, a cell that comes back full
   (or times out) is split into four quarters and searched again.
2. **Select** - date/panorama filters, optional thinning to a minimum
   distance between images, and an optional cap on the number of images.
3. **Download** - images are fetched in parallel into ``<output>/images``.
   Files already on disk are skipped, so re-running a job resumes it.
   ``metadata.csv`` / ``metadata.geojson`` describe every image on disk.
"""

from __future__ import annotations

import csv
import json
import math
import random
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

import requests

from .api import METADATA_FIELDS, SEARCH_LIMIT, THUMB_FIELDS, ApiError, MapillaryClient

INITIAL_CELL_DEG = 0.01  # ~1.1 km north-south
MIN_CELL_DEG = 0.0005  # ~55 m; cells are not split below this size
MAX_INITIAL_CELLS = 20_000  # ~ a 140 x 140 km area

CSV_COLUMNS = [
    "image_id",
    "file",
    "lat",
    "lon",
    "altitude",
    "heading",
    "captured_at",
    "captured_at_ms",
    "position_source",
    "lat_original",
    "lon_original",
    "heading_original",
    "camera_type",
    "is_pano",
    "make",
    "model",
    "width",
    "height",
    "sequence_id",
    "creator",
    "source",
    "license",
]

PANO_CAMERA_TYPES = {"spherical", "equirectangular"}


class Cancelled(Exception):
    pass


@dataclass
class JobConfig:
    bbox: tuple[float, float, float, float]  # west, south, east, north
    output_dir: Path
    image_size: str = "1024"
    start_date: str | None = None  # YYYY-MM-DD, inclusive
    end_date: str | None = None  # YYYY-MM-DD, inclusive
    include_panoramas: bool = False
    min_distance_m: float = 0.0
    max_images: int | None = None
    dry_run: bool = False
    search_workers: int = 4
    download_workers: int = 8

    def validate(self) -> None:
        if len(self.bbox) != 4:
            raise ValueError("bbox must be [west, south, east, north]")
        west, south, east, north = self.bbox
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise ValueError("bbox must satisfy west < east and south < north")
        if self.image_size not in THUMB_FIELDS:
            raise ValueError(f"image_size must be one of {sorted(THUMB_FIELDS)}")
        for name in ("start_date", "end_date"):
            value = getattr(self, name)
            if value:
                # Normalise to YYYY-MM-DD: fromisoformat() also accepts e.g.
                # "20240101", which would break the API query and the
                # string comparison below.
                setattr(self, name, date.fromisoformat(value).isoformat())
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if not math.isfinite(self.min_distance_m) or self.min_distance_m < 0:
            raise ValueError("min_distance_m must be a number >= 0")
        if self.max_images is not None and self.max_images < 1:
            raise ValueError("max_images must be >= 1")
        if count_cells(self.bbox, INITIAL_CELL_DEG) > MAX_INITIAL_CELLS:
            raise ValueError("Area is too large - select a smaller region")


# --------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------


def _grid_shape(bbox: Iterable[float], max_size: float) -> tuple[int, int]:
    west, south, east, north = bbox
    nx = max(1, math.ceil((east - west) / max_size - 1e-9))
    ny = max(1, math.ceil((north - south) / max_size - 1e-9))
    return nx, ny


def count_cells(bbox: Iterable[float], max_size: float) -> int:
    nx, ny = _grid_shape(bbox, max_size)
    return nx * ny


def split_bbox(bbox: Iterable[float], max_size: float) -> list[tuple[float, ...]]:
    west, south, east, north = bbox
    nx, ny = _grid_shape(bbox, max_size)
    dx, dy = (east - west) / nx, (north - south) / ny
    return [
        (west + i * dx, south + j * dy, west + (i + 1) * dx, south + (j + 1) * dy)
        for j in range(ny)
        for i in range(nx)
    ]


def quarter(bbox: tuple[float, ...]) -> list[tuple[float, ...]]:
    west, south, east, north = bbox
    mx, my = (west + east) / 2, (south + north) / 2
    return [
        (west, south, mx, my),
        (mx, south, east, my),
        (west, my, mx, north),
        (mx, my, east, north),
    ]


def thin_by_distance(records: list[dict], min_distance_m: float) -> list[dict]:
    """Keep images so that no two are closer than ``min_distance_m``.

    Newer images are preferred. Uses a local equirectangular projection and
    a grid hash, which is accurate enough for city/region sized areas.
    """
    if min_distance_m <= 0 or not records:
        return list(records)
    lat0 = sum(r["lat"] for r in records) / len(records)
    kx = 111_320 * math.cos(math.radians(lat0))
    ky = 110_540
    grid: dict[tuple[int, int], list[tuple[float, float]]] = {}
    kept = []
    for r in sorted(records, key=lambda r: r.get("captured_at_ms") or 0, reverse=True):
        x, y = r["lon"] * kx, r["lat"] * ky
        gx, gy = int(x // min_distance_m), int(y // min_distance_m)
        too_close = any(
            (x - px) ** 2 + (y - py) ** 2 < min_distance_m**2
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for px, py in grid.get((gx + dx, gy + dy), ())
        )
        if not too_close:
            grid.setdefault((gx, gy), []).append((x, y))
            kept.append(r)
    return kept


# --------------------------------------------------------------------------
# Record conversion
# --------------------------------------------------------------------------


def _parse_captured_at(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(value)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None  # one odd timestamp must not abort the whole search
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _iso(ms: int | None) -> str:
    if ms is None:
        return ""
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def normalize_image(item: dict, url_field: str) -> dict | None:
    """Turn a Graph API image object into a flat record (or None if unusable)."""
    computed = (item.get("computed_geometry") or {}).get("coordinates")
    original = (item.get("geometry") or {}).get("coordinates")
    coords = computed or original
    if not coords or "id" not in item:
        return None
    captured_ms = _parse_captured_at(item.get("captured_at"))
    heading = item.get("computed_compass_angle")
    if heading is None:
        heading = item.get("compass_angle")
    altitude = item.get("computed_altitude")
    if altitude is None:
        altitude = item.get("altitude")
    camera_type = item.get("camera_type") or ""
    creator = item.get("creator") or {}
    return {
        "image_id": str(item["id"]),
        "file": "",
        "lat": float(coords[1]),
        "lon": float(coords[0]),
        "altitude": altitude,
        "heading": heading,
        "captured_at": _iso(captured_ms),
        "captured_at_ms": captured_ms,
        "position_source": "computed" if computed else "original",
        "lat_original": original[1] if original else None,
        "lon_original": original[0] if original else None,
        "heading_original": item.get("compass_angle"),
        "camera_type": camera_type,
        "is_pano": camera_type in PANO_CAMERA_TYPES,
        "make": item.get("make") or "",
        "model": item.get("model") or "",
        "width": item.get("width"),
        "height": item.get("height"),
        "sequence_id": item.get("sequence") or "",
        "creator": creator.get("username", "") if isinstance(creator, dict) else "",
        "source": "mapillary",
        "license": "CC BY-SA 4.0",
        "url": item.get(url_field),
    }


def _day_bounds_ms(start_date: str | None, end_date: str | None):
    lo = hi = None
    if start_date:
        d = date.fromisoformat(start_date)
        lo = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
    if end_date:
        d = date.fromisoformat(end_date)
        hi = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
        hi += 86_400_000 - 1
    return lo, hi


# --------------------------------------------------------------------------
# Job
# --------------------------------------------------------------------------


@dataclass
class JobStats:
    cells_total: int = 0
    cells_done: int = 0
    found: int = 0
    selected: int = 0
    to_download: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    bytes: int = 0


@dataclass
class Job:
    config: JobConfig
    client: MapillaryClient
    name: str = ""
    http: requests.Session = field(default_factory=requests.Session)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "queued"
    error: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    stats: JobStats = field(default_factory=JobStats)

    def __post_init__(self):
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._log: list[str] = []
        self.points: list[tuple[str, float, float]] = []
        self.downloaded_ids: set[str] = set()
        self._retry_sleep = 2.0

    # -- public API ---------------------------------------------------------

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def active(self) -> bool:
        return self.status in ("queued", "searching", "downloading")

    def snapshot(self) -> dict:
        with self._lock:
            cfg = asdict(self.config)
            cfg["output_dir"] = str(self.config.output_dir)
            return {
                "id": self.id,
                "name": self.name,
                "status": self.status,
                "error": self.error,
                "created_at": self.created_at,
                "finished_at": self.finished_at,
                "config": cfg,
                "stats": asdict(self.stats),
                "log": self._log[-100:],
            }

    def point_list(self) -> list[list]:
        with self._lock:
            return [
                [image_id, lat, lon, image_id in self.downloaded_ids]
                for image_id, lat, lon in self.points
            ]

    def run(self) -> None:
        records: list[dict] = []
        final = "done"
        try:
            self._set_status("searching")
            found = self._search()
            records = self._select(found)
            if self.config.dry_run:
                self.log(f"Dry run finished: {len(records)} images would be downloaded.")
                return
            self._set_status("downloading")
            self._download(records)
            if self._cancel.is_set():
                self.log("Job cancelled.")
                final = "cancelled"
        except Cancelled:
            self.log("Job cancelled.")
            final = "cancelled"
        except Exception as exc:  # noqa: BLE001 - report every failure in the UI
            self.error = str(exc)
            self.log(f"ERROR: {exc}")
            final = "failed"
        finally:
            self.finished_at = time.time()
            if records and not self.config.dry_run:
                try:
                    self._write_metadata(records, final)
                except OSError as exc:
                    self.log(f"ERROR writing metadata: {exc}")
            self._set_status(final)

    # -- internals ----------------------------------------------------------

    def log(self, message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {message}"
        with self._lock:
            self._log.append(line)

    def _set_status(self, status: str) -> None:
        with self._lock:
            self.status = status

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    def _search(self) -> dict[str, dict]:
        cfg = self.config
        url_field = THUMB_FIELDS[cfg.image_size]
        fields = METADATA_FIELDS + [url_field]
        start = f"{cfg.start_date}T00:00:00Z" if cfg.start_date else None
        end = f"{cfg.end_date}T23:59:59Z" if cfg.end_date else None

        cells = split_bbox(cfg.bbox, INITIAL_CELL_DEG)
        with self._lock:
            self.stats.cells_total = len(cells)
        self.log(f"Searching {len(cells)} area cell(s) for images...")

        def search(cell):
            self._check_cancel()
            return self.client.search_images(
                cell, fields, start_captured_at=start, end_captured_at=end, retries=2
            )

        found: dict[str, dict] = {}
        truncated = 0
        with ThreadPoolExecutor(max_workers=cfg.search_workers) as pool:
            pending = {pool.submit(search, c): c for c in cells}
            try:
                while pending:
                    self._check_cancel()
                    done, _ = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                    for fut in done:
                        cell = pending.pop(fut)
                        split = False
                        try:
                            items = fut.result()
                        except ApiError as exc:
                            # Large/dense cells sometimes time out (server- or client-side).
                            server_error = exc.status is not None and exc.status >= 500
                            if (server_error or exc.timeout) and _can_split(cell):
                                split = True
                                items = []
                            else:
                                raise
                        if len(items) >= SEARCH_LIMIT:
                            if _can_split(cell):
                                split = True
                            else:
                                truncated += 1
                        if split:
                            children = quarter(cell)
                            for child in children:
                                pending[pool.submit(search, child)] = child
                            with self._lock:
                                self.stats.cells_total += len(children)
                                self.stats.cells_done += 1
                            continue
                        for item in items:
                            rec = normalize_image(item, url_field)
                            if rec:
                                found[rec["image_id"]] = rec
                        with self._lock:
                            self.stats.cells_done += 1
                            self.stats.found = len(found)
            finally:
                for fut in pending:
                    fut.cancel()
        if truncated:
            self.log(
                f"WARNING: {truncated} very dense cell(s) hit the API limit; "
                "some images there may be missing."
            )
        self.log(f"Search finished: {len(found)} images found.")
        return found

    def _select(self, found: dict[str, dict]) -> list[dict]:
        cfg = self.config
        lo, hi = _day_bounds_ms(cfg.start_date, cfg.end_date)
        records = []
        for rec in found.values():
            ms = rec["captured_at_ms"]
            if (lo is not None or hi is not None) and ms is None:
                continue
            if lo is not None and ms < lo:
                continue
            if hi is not None and ms > hi:
                continue
            if rec["is_pano"] and not cfg.include_panoramas:
                continue
            records.append(rec)
        if len(records) != len(found):
            self.log(f"{len(records)} images left after date/panorama filters.")

        if cfg.min_distance_m > 0:
            records = thin_by_distance(records, cfg.min_distance_m)
            self.log(f"{len(records)} images left after {cfg.min_distance_m:g} m thinning.")

        if cfg.max_images and len(records) > cfg.max_images:
            # A random sample keeps the spatial spread of the whole area.
            records = random.Random(42).sample(records, cfg.max_images)
            self.log(f"Randomly sampled {len(records)} images (max images limit).")

        records.sort(key=lambda r: (r["sequence_id"], r["captured_at_ms"] or 0))
        with self._lock:
            self.stats.selected = len(records)
            self.points = [(r["image_id"], r["lat"], r["lon"]) for r in records]
        return records

    def _download(self, records: list[dict]) -> None:
        images_dir = self.config.output_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        todo = []
        for rec in records:
            path = images_dir / f"{rec['image_id']}.jpg"
            if path.is_file() and path.stat().st_size > 0:
                with self._lock:
                    self.stats.skipped += 1
                    self.downloaded_ids.add(rec["image_id"])
            else:
                todo.append(rec)
        with self._lock:
            self.stats.to_download = len(todo)
        if self.stats.skipped:
            self.log(f"{self.stats.skipped} images already on disk - skipped.")
        self.log(f"Downloading {len(todo)} images...")

        with ThreadPoolExecutor(max_workers=self.config.download_workers) as pool:
            futures = {pool.submit(self._download_one, rec, images_dir): rec for rec in todo}
            for fut in as_completed(futures):
                rec = futures[fut]
                try:
                    size = fut.result()
                except Exception as exc:  # noqa: BLE001
                    size = None
                    self.log(f"Image {rec['image_id']} failed: {exc}")
                if self._cancel.is_set():
                    continue
                with self._lock:
                    if size is None:
                        self.stats.failed += 1
                    else:
                        self.stats.downloaded += 1
                        self.stats.bytes += size
                        self.downloaded_ids.add(rec["image_id"])
        with self._lock:
            s = self.stats
            summary = f"Downloaded {s.downloaded}, skipped {s.skipped}, failed {s.failed}."
        self.log(summary)

    def _download_one(self, rec: dict, images_dir: Path) -> int | None:
        if self._cancel.is_set():
            return None
        image_id = rec["image_id"]
        url = rec.get("url")
        url_field = THUMB_FIELDS[self.config.image_size]
        for attempt in range(4):
            if self._cancel.is_set():
                return None
            if not url:
                # Thumbnail URLs are signed and expire - ask the API for a fresh one.
                url = self.client.get_image(image_id, ["id", url_field]).get(url_field)
                if not url:
                    raise RuntimeError("no image URL available")
            try:
                resp = self.http.get(url, timeout=60)
            except requests.RequestException:
                self._cancel.wait(self._retry_sleep * (attempt + 1))
                continue
            if resp.status_code == 200:
                dest = images_dir / f"{image_id}.jpg"
                tmp = dest.with_suffix(".jpg.part")
                tmp.write_bytes(resp.content)
                tmp.replace(dest)
                return len(resp.content)
            if resp.status_code in (403, 404, 410):
                url = None
            elif resp.status_code == 429 or resp.status_code >= 500:
                self._cancel.wait(self._retry_sleep * (attempt + 1))
            else:
                raise RuntimeError(f"HTTP {resp.status_code}")
        raise RuntimeError("giving up after retries")

    def _write_metadata(self, records: list[dict], final_status: str) -> None:
        out = self.config.output_dir
        out.mkdir(parents=True, exist_ok=True)
        images_dir = out / "images"
        csv_path = out / "metadata.csv"

        # Keep rows of earlier runs into the same folder (different areas/filters).
        rows: dict[str, dict] = {}
        if csv_path.is_file():
            with csv_path.open(newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    rows[row["image_id"]] = row
        for rec in records:
            rows[rec["image_id"]] = {**rec, "file": f"images/{rec['image_id']}.jpg"}
        rows = {k: v for k, v in rows.items() if (images_dir / f"{k}.jpg").is_file()}

        tmp = csv_path.with_suffix(".csv.tmp")
        with tmp.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for row in sorted(rows.values(), key=lambda r: r["image_id"]):
                writer.writerow({k: "" if row.get(k) is None else row.get(k) for k in CSV_COLUMNS})
        tmp.replace(csv_path)

        features = []
        for row in rows.values():
            props = {k: row.get(k) for k in CSV_COLUMNS if k not in ("lat", "lon")}
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [float(row["lon"]), float(row["lat"])],
                    },
                    "properties": props,
                }
            )
        geojson = {"type": "FeatureCollection", "features": features}
        (out / "metadata.geojson").write_text(json.dumps(geojson), encoding="utf-8")

        info = self.snapshot()
        info["status"] = final_status
        info.pop("log", None)
        (out / f"job_{self.id}.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
        self.log(f"Wrote metadata for {len(rows)} images to {csv_path}")


def _can_split(cell: tuple[float, ...]) -> bool:
    west, south, east, north = cell
    return min(east - west, north - south) / 2 >= MIN_CELL_DEG

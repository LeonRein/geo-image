# geo-image

Tools for building a dataset of street-level images with precise GPS positions,
to train a model that predicts **where a photo was taken** from the image alone.

| Part | Status |
| --- | --- |
| [`mapillary_downloader`](#mapillary-area-downloader) – web UI that downloads all Mapillary images in a map area | ✅ ready |
| Smartphone capture app (Android + iPhone, photo every N seconds / N meters / manual) | planned |

---

## Mapillary area downloader

A small local web app: pick an area on a map, press **Download**, and it fetches every
public [Mapillary](https://www.mapillary.com) image in that area together with its
position, compass heading and capture time.

### Setup

Requires Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m mapillary_downloader     # opens http://localhost:8000
```

Options: `--port 8000`, `--output downloads` (target folder), `--host 0.0.0.0`
(only if you want to reach it from other machines – there is no login), `--no-browser`.

### Mapillary access token

The API is free but needs a token:

1. Log in at [mapillary.com](https://www.mapillary.com) → **Dashboard** → **Developers**.
2. **Register application** (any name/URL, read access is enough).
3. Copy the **Client Token** (starts with `MLY|`).

Paste it into the web UI, or start the server with `MAPILLARY_TOKEN=MLY|... python -m mapillary_downloader`
so you don't have to enter it every time.

### Using the UI

1. **Choose an area** – search a place name, draw a rectangle with the ■ tool, or use the visible map area.
2. **Options**
   - *Image size*: 256 / 1024 / 2048 px wide or original. 1024 px is plenty for most models and ~150–300 KB per image.
   - *Captured from / until*: only images taken in this date range.
   - *Min. distance between images*: e.g. `10` keeps at most one image per 10 m (newest wins). Mapillary sequences
     often have a photo every 1–3 m, so this removes near-duplicates.
   - *Max. number of images*: random sample over the whole area.
   - *Include 360° panoramas*: off by default – panoramas look very different from normal phone photos.
3. **Count images** does the search only (nothing is downloaded) so you can see how many images you'd get.
4. **Download** – progress, a log and all found images (blue = pending, green = downloaded) appear on the map.
   Click a point to preview the image.

Re-running a download into the same folder **resumes**: files already on disk are skipped.
You can also download several areas into the same folder; the metadata files are merged.

### Output

```
downloads/<folder name>/
├── images/<image_id>.jpg
├── metadata.csv          # one row per image on disk
├── metadata.geojson      # same, as GeoJSON points (open in QGIS / geojson.io)
└── job_<id>.json         # settings and statistics of each run
```

`metadata.csv` columns:

| column | meaning |
| --- | --- |
| `image_id`, `file` | Mapillary image ID, relative path of the JPEG |
| `lat`, `lon` | position (WGS84). Uses Mapillary's **computed** (SfM-corrected) position when available |
| `position_source` | `computed` or `original` (raw GPS from the camera) |
| `lat_original`, `lon_original` | raw GPS position as uploaded |
| `heading`, `heading_original` | compass direction of the camera in degrees (computed / raw) |
| `altitude` | metres |
| `captured_at`, `captured_at_ms` | capture time (ISO 8601 UTC / Unix ms) |
| `camera_type`, `is_pano`, `make`, `model`, `width`, `height` | camera info |
| `sequence_id`, `creator` | Mapillary sequence and uploader username |
| `source`, `license` | `mapillary`, `CC BY-SA 4.0` |

### How it works

The Mapillary Graph API returns at most 2000 images per `GET /images?bbox=…` request.
The area is therefore cut into ~1 km cells; any cell that comes back full (or times out)
is split into four smaller cells and searched again until every image is found.
Image files come from Mapillary's CDN via signed URLs that expire – when a URL is
rejected, a fresh one is requested automatically.

### License & attribution

Mapillary images are licensed [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
Training your own model on them is fine; if you **publish** the images or a dataset,
credit Mapillary and the contributors (the `creator` column) and share it under the same license.
Also follow the [Mapillary Terms](https://www.mapillary.com/terms).
Faces and licence plates are blurred by Mapillary.

### Development

```bash
pip install -r requirements-dev.txt
pytest
```

The tests run against a local fake of the Mapillary API (`tests/fake_mapillary.py`), so no token or network is needed.
Leaflet and Leaflet.draw are vendored in `mapillary_downloader/static/vendor` so the UI works without a CDN.

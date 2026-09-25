import csv
import json
import threading

import pytest

from mapillary_downloader.api import MapillaryClient
from mapillary_downloader.downloader import (
    Job,
    JobConfig,
    normalize_image,
    quarter,
    split_bbox,
    thin_by_distance,
)
from tests.fake_mapillary import TOKEN, FakeMapillary, make_image

BBOX = (7.80, 47.99, 7.82, 48.00)  # 2 x 1 initial cells


def grid_images(n, bbox=BBOX, start_id=1, **kw):
    """n images spread evenly inside bbox."""
    w, s, e, n_ = bbox
    side = int(n**0.5) + 1
    images = []
    for i in range(n):
        x, y = i % side, i // side
        lon = w + (e - w) * (x + 0.5) / side
        lat = s + (n_ - s) * (y + 0.5) / side
        images.append(make_image(start_id + i, lon, lat, captured_at=1_600_000_000_000 + i, **kw))
    return images


def run_job(fake, tmp_path, token=TOKEN, **kw):
    kw.setdefault("bbox", BBOX)
    config = JobConfig(output_dir=tmp_path / "out", **kw)
    config.validate()
    client = MapillaryClient(token, base_url=fake.base_url, sleep=lambda s: None)
    job = Job(config=config, client=client, name="test")
    job._retry_sleep = 0
    job.run()
    return job


def read_csv(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------- helpers


def test_split_bbox_covers_area_without_gaps():
    cells = split_bbox((0, 0, 0.025, 0.01), 0.01)
    assert len(cells) == 3
    assert cells[0][0] == 0 and cells[-1][2] == pytest.approx(0.025)
    for a, b in zip(cells, cells[1:]):
        assert a[2] == pytest.approx(b[0])
    assert split_bbox((0, 0, 0.01, 0.01), 0.01) == [(0, 0, 0.01, 0.01)]


def test_quarter():
    assert quarter((0, 0, 2, 2)) == [(0, 0, 1, 1), (1, 0, 2, 1), (0, 1, 1, 2), (1, 1, 2, 2)]


def test_thin_by_distance_prefers_newer_images():
    old = {"lat": 48.0, "lon": 7.8, "captured_at_ms": 1}
    new = {"lat": 48.00001, "lon": 7.8, "captured_at_ms": 2}  # ~1 m away
    far = {"lat": 48.001, "lon": 7.8, "captured_at_ms": 0}  # ~110 m away
    kept = thin_by_distance([old, new, far], 10)
    assert new in kept and far in kept and old not in kept
    assert len(thin_by_distance([old, new, far], 0)) == 3


def test_normalize_image_falls_back_to_original_geometry():
    rec = normalize_image(make_image(5, 7.8, 48.0, computed=False, camera_type="spherical"), "thumb_1024_url")
    assert rec["position_source"] == "original"
    assert rec["lon"] == pytest.approx(7.80001)
    assert rec["is_pano"] is True
    assert rec["heading"] == 91.5
    assert rec["captured_at"] == "2023-11-14T22:13:20.000Z"
    assert normalize_image({"id": "1"}, "thumb_1024_url") is None


# ---------------------------------------------------------------- jobs


def test_download_writes_images_and_metadata(tmp_path):
    images = grid_images(20)
    with FakeMapillary(images) as fake:
        job = run_job(fake, tmp_path, image_size="2048")
    assert job.status == "done", job.snapshot()
    assert job.stats.found == 20 and job.stats.downloaded == 20 and job.stats.failed == 0
    out = tmp_path / "out"
    assert (out / "images" / "1.jpg").read_bytes() == b"JPEG:1:thumb_2048_url"

    rows = read_csv(out / "metadata.csv")
    assert len(rows) == 20
    row = next(r for r in rows if r["image_id"] == "1")
    assert row["file"] == "images/1.jpg"
    assert row["position_source"] == "computed"
    assert float(row["heading"]) == 91.5
    assert row["creator"] == "alice"
    assert row["license"] == "CC BY-SA 4.0"
    assert float(row["lat"]) == pytest.approx(images[0]["computed_geometry"]["coordinates"][1])

    geo = json.loads((out / "metadata.geojson").read_text())
    assert len(geo["features"]) == 20
    assert list((out).glob("job_*.json"))


def test_rerun_skips_existing_files_and_keeps_old_rows(tmp_path):
    with FakeMapillary(grid_images(10)) as fake:
        run_job(fake, tmp_path)
        fake.downloads.clear()
        job = run_job(fake, tmp_path)
        assert fake.downloads == []
    assert job.stats.skipped == 10 and job.stats.downloaded == 0

    # A second, disjoint area into the same folder keeps the first area's rows.
    other = (7.83, 47.99, 7.84, 48.00)
    with FakeMapillary(grid_images(10) + grid_images(5, bbox=other, start_id=100)) as fake:
        run_job(fake, tmp_path, bbox=other)
    assert len(read_csv(tmp_path / "out" / "metadata.csv")) == 15


def test_expired_urls_are_refreshed(tmp_path):
    with FakeMapillary(grid_images(5), expired_ids={"2", "4"}) as fake:
        job = run_job(fake, tmp_path)
        assert sorted(fake.refreshes) == ["2", "4"]
    assert job.stats.downloaded == 5


def test_full_cells_are_split(tmp_path):
    # 4500 images in a single 0.01 deg cell -> first query hits the 2000 limit.
    cell = (7.80, 47.99, 7.81, 48.00)
    with FakeMapillary(grid_images(4500, bbox=cell)) as fake:
        job = run_job(fake, tmp_path, bbox=cell, dry_run=True)
        assert len(fake.search_calls) > 1
    assert job.status == "done"
    assert job.stats.found == 4500
    assert job.stats.cells_done == job.stats.cells_total
    assert not (tmp_path / "out").exists()  # dry run writes nothing


def test_server_errors_on_large_cells_trigger_split(tmp_path):
    with FakeMapillary(grid_images(30), fail_wider_than=0.006) as fake:
        job = run_job(fake, tmp_path, dry_run=True)
    assert job.status == "done", job.error
    assert job.stats.found == 30


def test_filters_and_limits(tmp_path):
    images = grid_images(10)
    images[0]["camera_type"] = "spherical"
    images[1]["captured_at"] = 1_500_000_000_000  # 2017
    with FakeMapillary(images) as fake:
        job = run_job(fake, tmp_path, dry_run=True, start_date="2020-01-01")
        assert job.stats.found == 10 and job.stats.selected == 8
        job = run_job(fake, tmp_path, dry_run=True, include_panoramas=True, max_images=3)
        assert job.stats.selected == 3
        # All images lie ~150 m apart; a 1 km minimum distance keeps just one.
        job = run_job(fake, tmp_path, dry_run=True, min_distance_m=1000)
        assert job.stats.selected == 1


def test_invalid_token_fails_job_with_message(tmp_path):
    with FakeMapillary(grid_images(3)) as fake:
        job = run_job(fake, tmp_path, token="MLY|wrong")
    assert job.status == "failed"
    assert "access token" in job.error


def test_cancel_stops_job(tmp_path):
    with FakeMapillary(grid_images(50)) as fake:
        config = JobConfig(bbox=BBOX, output_dir=tmp_path / "out", download_workers=1)
        client = MapillaryClient(TOKEN, base_url=fake.base_url, sleep=lambda s: None)
        job = Job(config=config, client=client)
        original = job._download_one

        def slow_download(rec, images_dir):
            result = original(rec, images_dir)
            if len(job.downloaded_ids) >= 5 or job.stats.downloaded >= 5:
                job.cancel()
            return result

        job._download_one = slow_download
        t = threading.Thread(target=job.run)
        t.start()
        t.join(timeout=30)
    assert job.status == "cancelled"
    assert job.stats.downloaded < 50
    rows = read_csv(tmp_path / "out" / "metadata.csv")
    assert len(rows) == len(list((tmp_path / "out" / "images").glob("*.jpg")))


@pytest.mark.parametrize(
    "kw",
    [
        {"bbox": (8, 48, 7, 49)},
        {"image_size": "999"},
        {"start_date": "2024-02-30"},
        {"start_date": "2024-02-01", "end_date": "2024-01-01"},
        {"bbox": (-180, -80, 180, 80)},
    ],
)
def test_config_validation(kw, tmp_path):
    kw.setdefault("bbox", BBOX)
    with pytest.raises(ValueError):
        JobConfig(output_dir=tmp_path, **kw).validate()

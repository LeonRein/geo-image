import time

import pytest

from mapillary_downloader.api import MapillaryClient
from mapillary_downloader.server import create_app, sanitize_name
from tests.fake_mapillary import TOKEN, FakeMapillary
from tests.test_downloader import BBOX, grid_images


@pytest.fixture
def fake():
    with FakeMapillary(grid_images(12)) as fake:
        yield fake


@pytest.fixture
def client(fake, tmp_path):
    app = create_app(
        output_root=tmp_path,
        default_token="",
        client_factory=lambda token: MapillaryClient(token, base_url=fake.base_url, sleep=lambda s: None),
    )
    return app.test_client()


def wait_for(client, job_id, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").get_json()
        if job["status"] not in ("queued", "searching", "downloading"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_index_and_config(client):
    assert b"Mapillary Area Downloader" in client.get("/").data
    assert client.get("/static/vendor/leaflet/leaflet.js").status_code == 200
    assert client.get("/api/config").get_json()["has_server_token"] is False


def test_full_download_flow(client, tmp_path):
    res = client.post("/api/jobs", json={"bbox": list(BBOX), "token": TOKEN, "name": "../../Freiburg Süd"})
    assert res.status_code == 201, res.get_json()
    job = wait_for(client, res.get_json()["id"])
    assert job["status"] == "done", job
    assert job["stats"]["downloaded"] == 12
    out = tmp_path / job["name"]
    assert out.parent == tmp_path  # no path traversal via the folder name
    assert (out / "metadata.csv").is_file()

    points = client.get(f"/api/jobs/{job['id']}/points").get_json()
    assert len(points) == 12 and all(p[3] for p in points)
    img = client.get(f"/api/jobs/{job['id']}/images/{points[0][0]}")
    assert img.status_code == 200 and img.data.startswith(b"JPEG:")
    assert client.get(f"/api/jobs/{job['id']}/images/..%2Fmetadata.csv").status_code == 404

    jobs = client.get("/api/jobs").get_json()
    assert [j["id"] for j in jobs] == [job["id"]]
    assert "token" not in str(jobs)


def test_validation_errors(client):
    assert client.post("/api/jobs", json={"bbox": list(BBOX)}).status_code == 400  # no token
    res = client.post("/api/jobs", json={"bbox": [1, 2, 3], "token": TOKEN})
    assert res.status_code == 400 and "bbox" in res.get_json()["error"]
    res = client.post("/api/jobs", json={"bbox": list(BBOX), "token": TOKEN, "image_size": "5"})
    assert res.status_code == 400
    assert client.get("/api/jobs/nope").status_code == 404


def test_only_one_job_at_a_time(client, fake):
    first = client.post("/api/jobs", json={"bbox": list(BBOX), "token": TOKEN, "dry_run": True}).get_json()
    second = client.post("/api/jobs", json={"bbox": list(BBOX), "token": TOKEN, "dry_run": True})
    if second.status_code == 201:
        # The first job was fast enough to finish already - that is fine too.
        assert wait_for(client, first["id"])["status"] == "done"
    else:
        assert second.status_code == 409
    wait_for(client, first["id"])


def test_sanitize_name():
    assert sanitize_name("../etc/passwd") == "etc_passwd"
    assert sanitize_name("My Town 2024") == "My_Town_2024"
    assert sanitize_name("").startswith("area_")

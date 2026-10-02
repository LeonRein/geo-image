"""Local web UI + JSON API for the Mapillary area downloader."""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from .api import MapillaryClient
from .downloader import Job, JobConfig

STATIC_DIR = Path(__file__).parent / "static"
NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
IMAGE_ID_RE = re.compile(r"^[0-9]+$")


class JobManager:
    def __init__(self, output_root: Path, client_factory: Callable[[str], MapillaryClient]):
        self.output_root = output_root
        self.client_factory = client_factory
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(self, config: JobConfig, token: str, name: str) -> Job:
        with self._lock:
            if any(job.active for job in self.jobs.values()):
                raise RuntimeError("Another job is still running - wait or cancel it first.")
            job = Job(config=config, client=self.client_factory(token), name=name)
            self.jobs[job.id] = job
        threading.Thread(target=job.run, name=f"job-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            abort(404, "Unknown job")
        return job


def sanitize_name(name: str) -> str:
    name = NAME_RE.sub("_", name.strip()).strip("._")
    return name[:80] or time.strftime("area_%Y%m%d_%H%M%S")


def create_app(
    output_root: str | Path = "downloads",
    default_token: str | None = None,
    client_factory: Callable[[str], MapillaryClient] = MapillaryClient,
) -> Flask:
    output_root = Path(output_root).resolve()
    default_token = default_token if default_token is not None else os.environ.get("MAPILLARY_TOKEN", "")
    manager = JobManager(output_root, client_factory)
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.config["manager"] = manager

    @app.errorhandler(HTTPException)
    def json_error(err):
        # The UI always expects JSON, also for 405/500 etc.
        return jsonify(error=err.description), err.code

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/api/config")
    def config():
        return jsonify(has_server_token=bool(default_token), output_root=str(output_root))

    @app.get("/api/jobs")
    def list_jobs():
        jobs = sorted(manager.jobs.values(), key=lambda j: j.created_at, reverse=True)
        return jsonify([job.snapshot() for job in jobs])

    @app.post("/api/jobs")
    def create_job():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            abort(400, "Expected a JSON object.")
        token = (body.get("token") or "").strip() or default_token
        if not token:
            abort(400, "A Mapillary access token is required.")
        name = sanitize_name(body.get("name") or "")
        try:
            max_images = body.get("max_images")
            config = JobConfig(
                bbox=tuple(float(v) for v in body.get("bbox") or ()),
                output_dir=output_root / name,
                image_size=str(body.get("image_size") or "1024"),
                start_date=body.get("start_date") or None,
                end_date=body.get("end_date") or None,
                include_panoramas=bool(body.get("include_panoramas")),
                min_distance_m=float(body.get("min_distance_m") or 0),
                max_images=int(max_images) if max_images not in (None, "", 0) else None,
                dry_run=bool(body.get("dry_run")),
            )
            config.validate()
        except (TypeError, ValueError) as exc:
            abort(400, str(exc))
        try:
            job = manager.start(config, token, name)
        except RuntimeError as exc:
            abort(409, str(exc))
        return jsonify(job.snapshot()), 201

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id):
        return jsonify(manager.get(job_id).snapshot())

    @app.get("/api/jobs/<job_id>/points")
    def job_points(job_id):
        return jsonify(manager.get(job_id).point_list())

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel_job(job_id):
        job = manager.get(job_id)
        job.cancel()
        return jsonify(job.snapshot())

    @app.get("/api/jobs/<job_id>/images/<image_id>")
    def job_image(job_id, image_id):
        job = manager.get(job_id)
        if not IMAGE_ID_RE.match(image_id):
            abort(404, "Unknown image")
        return send_from_directory(job.config.output_dir / "images", f"{image_id}.jpg")

    return app

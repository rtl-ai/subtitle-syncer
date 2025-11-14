from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.services import CommandResult, PipelineError


@pytest.fixture(autouse=True)
def clean_jobs(tmp_path, monkeypatch):
    base_dir = tmp_path / "jobs"
    base_dir.mkdir()
    monkeypatch.setattr(main, "BASE_DIR", base_dir)
    monkeypatch.setattr(main, "JOB_TTL_SECONDS", 10)
    main.job_registry.clear()
    yield
    main.job_registry.clear()


def test_process_success(monkeypatch, tmp_path):
    client = TestClient(main.app)

    def fake_run_uchardet(path: Path):
        return "utf-8", CommandResult(["uchardet"], "utf-8", "")

    def fake_run_pysubs2(input_path: Path, output_path: Path, encoding: str, cwd=None):
        output_path.write_text("normalized")
        return CommandResult(["pysubs2"], "converted", "")

    def fake_run_ffsubsync(video_path: Path, subtitle_path: Path, output_path: Path, cwd=None):
        output_path.write_text("aligned")
        return CommandResult(["ffsubsync"], "synced", "")

    monkeypatch.setattr(main, "run_uchardet", fake_run_uchardet)
    monkeypatch.setattr(main, "run_pysubs2", fake_run_pysubs2)
    monkeypatch.setattr(main, "run_ffsubsync", fake_run_ffsubsync)
    monkeypatch.setattr(main, "_schedule_job_cleanup", lambda *args, **kwargs: None)

    files = {
        "video_file": ("movie.mp4", b"video", "video/mp4"),
        "subtitle_file": ("subs.srt", b"1-->2", "text/plain"),
    }

    response = client.post("/process", files=files)
    assert response.status_code == 200
    html = response.text
    assert "Download synced subtitle" in html

    match = re.search(r"/download/([a-f0-9]+)", html)
    assert match
    job_id = match.group(1)

    final_path = main.BASE_DIR / job_id / "movie.srt"
    assert final_path.exists()
    assert final_path.read_text() == "aligned"


def test_process_failure(monkeypatch):
    client = TestClient(main.app)

    def fake_run_uchardet(path: Path):
        raise PipelineError("Failed")

    monkeypatch.setattr(main, "run_uchardet", fake_run_uchardet)
    monkeypatch.setattr(main, "_schedule_job_cleanup", lambda *args, **kwargs: None)

    files = {
        "video_file": ("movie.mp4", b"video", "video/mp4"),
        "subtitle_file": ("subs.srt", b"1-->2", "text/plain"),
    }

    response = client.post("/process", files=files)
    assert response.status_code == 500
    assert "Processing failed" in response.text

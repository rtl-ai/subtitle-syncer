from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import UploadFile
from starlette.background import BackgroundTasks
from starlette.requests import Request

from app import main
from app.services import CommandResult, PipelineError


def build_request(method: str = "GET", path: str = "/") -> Request:
    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [],
        "client": ("test", 123),
        "server": ("testserver", 80),
        "app": main.app,
    }
    return Request(scope, receive)


@pytest.fixture(autouse=True)
def clean_jobs(tmp_path, monkeypatch):
    base_dir = tmp_path / "jobs"
    base_dir.mkdir()
    monkeypatch.setattr(main, "BASE_DIR", base_dir)
    monkeypatch.setattr(main, "JOB_TTL_SECONDS", 10)
    main.job_registry.clear()
    yield
    main.job_registry.clear()


@pytest.mark.anyio("asyncio")
async def test_process_success(monkeypatch, tmp_path):
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

    async def noop_cleanup(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "_schedule_job_cleanup", noop_cleanup)

    video_upload = UploadFile(file=BytesIO(b"video"), filename="movie.mp4")
    subtitle_upload = UploadFile(file=BytesIO(b"1-->2"), filename="subs.srt")

    background_tasks = BackgroundTasks()
    request = build_request("POST", "/process")

    response = await main.process_request(
        request,
        background_tasks,
        video_upload,
        subtitle_upload,
        encoding_override="",
        force_sami=False,
    )
    assert response.status_code == 202
    html = response.body.decode()
    match = re.search(r"data-job-id=\"([a-f0-9]+)\"", html)
    assert match
    job_id = match.group(1)

    await background_tasks()

    status_payload = await main.job_status(job_id)
    assert status_payload["status"] == "completed"
    assert any(log["name"] == "ffsubsync" for log in status_payload["logs"])
    assert any(event["current_step"] == "Aligned subtitles" for event in status_payload["events"])

    result_request = build_request("GET", f"/result/{job_id}")
    result_response = await main.job_result(result_request, job_id)
    assert result_response.status_code == 200
    assert "Download synced subtitle" in result_response.body.decode()
    assert "Processing timeline" in result_response.body.decode()

    final_path = main.BASE_DIR / job_id / "movie.srt"
    assert final_path.exists()
    assert final_path.read_text() == "aligned"

    backup_path = main.BASE_DIR / job_id / "movie.srt.bk"
    assert backup_path.exists()

    zip_response = await main.download_zip(job_id)
    assert zip_response.status_code == 200


@pytest.mark.anyio("asyncio")
async def test_process_failure(monkeypatch):
    def fake_run_uchardet(path: Path):
        raise PipelineError("Failed")

    monkeypatch.setattr(main, "run_uchardet", fake_run_uchardet)

    async def noop_cleanup(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "_schedule_job_cleanup", noop_cleanup)

    video_upload = UploadFile(file=BytesIO(b"video"), filename="movie.mp4")
    subtitle_upload = UploadFile(file=BytesIO(b"1-->2"), filename="subs.srt")

    background_tasks = BackgroundTasks()
    request = build_request("POST", "/process")

    response = await main.process_request(
        request,
        background_tasks,
        video_upload,
        subtitle_upload,
        encoding_override="",
        force_sami=False,
    )
    assert response.status_code == 202
    html = response.body.decode()
    match = re.search(r"data-job-id=\"([a-f0-9]+)\"", html)
    assert match
    job_id = match.group(1)

    await background_tasks()

    status_payload = await main.job_status(job_id)
    assert status_payload["status"] == "failed"
    assert status_payload["logs"] == []

    result_request = build_request("GET", f"/result/{job_id}")
    error_response = await main.job_result(result_request, job_id)
    assert error_response.status_code == 500
    assert "Processing failed" in error_response.body.decode()
    assert "Timeline" in error_response.body.decode()
    assert not list(main.BASE_DIR.iterdir())


@pytest.mark.anyio("asyncio")
async def test_cleanup_on_upload_failure(monkeypatch):
    from fastapi import HTTPException

    async def raise_on_save(*args, **kwargs):
        raise HTTPException(status_code=413, detail="Too large")

    monkeypatch.setattr(main, "_save_upload_file", raise_on_save)

    video_upload = UploadFile(file=BytesIO(b"video"), filename="movie.mp4")
    subtitle_upload = UploadFile(file=BytesIO(b"1-->2"), filename="subs.srt")

    background_tasks = BackgroundTasks()
    request = build_request("POST", "/process")

    with pytest.raises(HTTPException):
        await main.process_request(
            request,
            background_tasks,
            video_upload,
            subtitle_upload,
            encoding_override="",
            force_sami=False,
        )

    assert not list(main.BASE_DIR.iterdir())

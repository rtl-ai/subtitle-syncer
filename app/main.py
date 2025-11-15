from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .services import (
    CommandResult,
    PipelineError,
    cleanup_old_jobs,
    run_ffsubsync,
    run_pysubs2,
    run_uchardet,
)

BASE_DIR = Path("/tmp/subsync_jobs")
BASE_DIR.mkdir(parents=True, exist_ok=True)
JOB_TTL_SECONDS = 60 * 60  # 1 hour
MAX_VIDEO_SIZE = 8 * 1024 * 1024 * 1024  # 8 GB
MAX_SUBTITLE_SIZE = 20 * 1024 * 1024  # 20 MB
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov"}
SUBTITLE_EXTENSIONS = {".smi", ".srt", ".ass", ".ssa", ".sub", ".vtt"}


@dataclass
class JobResult:
    job_id: str
    created_at: datetime
    video_basename: str
    final_subtitle_path: Path
    log_messages: Dict[str, CommandResult]
    detected_encoding: str
    zip_path: Optional[Path]


job_registry: Dict[str, JobResult] = {}
registry_lock = asyncio.Lock()

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app = FastAPI(title="Subtitle Syncer", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


def _ensure_extension(filename: str, allowed: set[str]) -> None:
    extension = Path(filename).suffix.lower()
    if extension not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"File '{filename}' has an unsupported extension",
        )


def _sanitize_basename(filename: str) -> str:
    stem = Path(filename).stem
    return stem or "video"


async def _save_upload_file(upload: UploadFile, destination: Path, max_bytes: int) -> None:
    size = 0
    with destination.open("wb") as buffer:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"File '{upload.filename}' exceeds the size limit",
                )
            buffer.write(chunk)
    await upload.close()


def _backup_existing_subtitles(job_dir: Path, basename: str) -> None:
    for extension in (".srt", ".smi"):
        original = job_dir / f"{basename}{extension}"
        if original.exists():
            backup = original.with_suffix(original.suffix + ".bk")
            original.rename(backup)


def _create_zip_archive(files: Dict[str, Path], destination: Path) -> None:
    import zipfile

    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for arcname, file_path in files.items():
            if file_path.exists():
                archive.write(file_path, arcname=arcname)


async def _store_job_result(result: JobResult) -> None:
    async with registry_lock:
        job_registry[result.job_id] = result


async def _remove_job(job_id: str) -> None:
    async with registry_lock:
        job_registry.pop(job_id, None)


def _safe_cleanup(job_dir: Path) -> None:
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)


@app.on_event("startup")
async def startup_cleanup() -> None:
    await run_in_threadpool(cleanup_old_jobs, BASE_DIR, JOB_TTL_SECONDS)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/process")
async def process_request(
    request: Request,
    background_tasks: BackgroundTasks,
    video_file: UploadFile,
    subtitle_file: UploadFile,
    encoding_override: str = Form(default=""),
    force_sami: bool = Form(default=False),
):
    await run_in_threadpool(cleanup_old_jobs, BASE_DIR, JOB_TTL_SECONDS)

    if not video_file or not subtitle_file:
        raise HTTPException(status_code=400, detail="Both video and subtitle files are required")

    _ensure_extension(video_file.filename or "", VIDEO_EXTENSIONS)
    _ensure_extension(subtitle_file.filename or "", SUBTITLE_EXTENSIONS)

    job_id = uuid4().hex
    job_dir = BASE_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=False)

    video_path = job_dir / f"original_video{Path(video_file.filename or '').suffix.lower()}"
    subtitle_path = job_dir / f"original_sub{Path(subtitle_file.filename or '').suffix.lower()}"

    await _save_upload_file(video_file, video_path, MAX_VIDEO_SIZE)
    await _save_upload_file(subtitle_file, subtitle_path, MAX_SUBTITLE_SIZE)

    if force_sami and subtitle_path.suffix.lower() != ".smi":
        forced_subtitle_path = subtitle_path.with_suffix(".smi")
        subtitle_path.rename(forced_subtitle_path)
        subtitle_path = forced_subtitle_path

    video_basename = _sanitize_basename(video_file.filename or "video")
    final_subtitle_path = job_dir / f"{video_basename}.srt"
    normalized_subtitle_path = job_dir / "normalized_sub.srt"
    aligned_temp_path = job_dir / "aligned_temp.srt"

    logs: Dict[str, CommandResult] = {}

    input_encoding = encoding_override.strip().lower()
    try:
        detected_encoding = input_encoding
        if not detected_encoding:
            detected_encoding, uchardet_result = await run_in_threadpool(
                run_uchardet, subtitle_path
            )
            logs["uchardet"] = uchardet_result
        else:
            logs["uchardet"] = CommandResult(
                command=["encoding", "override"],
                stdout=f"User override: {detected_encoding}",
                stderr="",
            )

        command = await run_in_threadpool(
            run_pysubs2, subtitle_path, normalized_subtitle_path, detected_encoding
        )
        logs["pysubs2"] = command

        await run_in_threadpool(_backup_existing_subtitles, job_dir, video_basename)

        ffsubsync_result = await run_in_threadpool(
            run_ffsubsync, video_path, normalized_subtitle_path, aligned_temp_path
        )
        logs["ffsubsync"] = ffsubsync_result

        await run_in_threadpool(aligned_temp_path.replace, final_subtitle_path)

        zip_path: Optional[Path] = None
        files_for_zip: Dict[str, Path] = {f"{video_basename}.srt": final_subtitle_path}
        backups = [p for p in job_dir.glob(f"{video_basename}.srt.bk")]
        backups += [p for p in job_dir.glob(f"{video_basename}.smi.bk")]
        for backup in backups:
            files_for_zip[backup.name] = backup

        if len(files_for_zip) > 1:
            zip_path = job_dir / f"{video_basename}_results.zip"
            await run_in_threadpool(_create_zip_archive, files_for_zip, zip_path)

        result = JobResult(
            job_id=job_id,
            created_at=datetime.utcnow(),
            video_basename=video_basename,
            final_subtitle_path=final_subtitle_path,
            log_messages=logs,
            detected_encoding=detected_encoding,
            zip_path=zip_path,
        )
        await _store_job_result(result)

        background_tasks.add_task(_schedule_job_cleanup, job_id, job_dir)

        ttl_minutes = max(1, JOB_TTL_SECONDS // 60)
        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "job": result,
                "ttl_minutes": ttl_minutes,
            },
        )
    except PipelineError as exc:
        _safe_cleanup(job_dir)
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "error_message": str(exc),
                "logs": logs,
                "ttl_minutes": max(1, JOB_TTL_SECONDS // 60),
            },
            status_code=500,
        )
    except Exception:
        _safe_cleanup(job_dir)
        raise


async def _schedule_job_cleanup(job_id: str, job_dir: Path) -> None:
    await asyncio.sleep(JOB_TTL_SECONDS)
    await _remove_job(job_id)
    if job_dir.exists():
        await run_in_threadpool(cleanup_old_jobs, BASE_DIR, JOB_TTL_SECONDS)


@app.get("/download/{job_id}")
async def download(job_id: str) -> FileResponse:
    job = await _get_job(job_id)
    return FileResponse(job.final_subtitle_path, filename=f"{job.video_basename}.srt")


@app.get("/download/{job_id}/zip")
async def download_zip(job_id: str) -> FileResponse:
    job = await _get_job(job_id)
    if not job.zip_path or not job.zip_path.exists():
        raise HTTPException(status_code=404, detail="Archive not available")
    return FileResponse(job.zip_path, filename=job.zip_path.name)


async def _get_job(job_id: str) -> JobResult:
    async with registry_lock:
        job = job_registry.get(job_id)
    if not job or not job.final_subtitle_path.exists():
        raise HTTPException(status_code=404, detail="Job not found or expired")
    return job


__all__ = ["app"]

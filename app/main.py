from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Literal, Optional
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
class JobState:
    job_id: str
    created_at: datetime
    status: Literal["queued", "running", "completed", "failed"]
    progress: float
    current_step: str
    message: str
    video_basename: Optional[str] = None
    final_subtitle_path: Optional[Path] = None
    log_messages: Dict[str, CommandResult] = field(default_factory=dict)
    detected_encoding: Optional[str] = None
    zip_path: Optional[Path] = None
    error_message: Optional[str] = None


job_registry: Dict[str, JobState] = {}
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


async def _register_job(job: JobState) -> None:
    async with registry_lock:
        job_registry[job.job_id] = job


async def _update_job(job_id: str, **changes: object) -> JobState:
    async with registry_lock:
        job = job_registry.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found or expired")
        for key, value in changes.items():
            setattr(job, key, value)
        return job


async def _append_log(job_id: str, name: str, entry: CommandResult) -> None:
    async with registry_lock:
        job = job_registry.get(job_id)
        if job:
            job.log_messages[name] = entry


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


async def _run_pipeline(
    job_id: str,
    job_dir: Path,
    video_path: Path,
    subtitle_path: Path,
    normalized_subtitle_path: Path,
    aligned_temp_path: Path,
    final_subtitle_path: Path,
    video_basename: str,
    detected_encoding: Optional[str],
) -> None:
    try:
        await _update_job(
            job_id,
            status="running",
            message="Detecting subtitle encoding…",
            current_step="Detecting encoding",
            progress=0.25,
        )

        encoding = detected_encoding
        if not encoding:
            encoding, uchardet_result = await run_in_threadpool(run_uchardet, subtitle_path)
            await _append_log(job_id, "uchardet", uchardet_result)
            await _update_job(job_id, detected_encoding=encoding)

        await _update_job(
            job_id,
            message="Normalizing subtitles with pysubs2…",
            current_step="Normalizing subtitles",
            progress=0.45,
        )
        pysubs2_result = await run_in_threadpool(
            run_pysubs2, subtitle_path, normalized_subtitle_path, encoding
        )
        await _append_log(job_id, "pysubs2", pysubs2_result)

        await _update_job(
            job_id,
            message="Creating backups…",
            current_step="Backing up existing subtitles",
            progress=0.6,
        )
        await run_in_threadpool(_backup_existing_subtitles, job_dir, video_basename)

        await _update_job(
            job_id,
            message="Aligning subtitles with ffsubsync…",
            current_step="Aligning subtitles",
            progress=0.8,
        )
        ffsubsync_result = await run_in_threadpool(
            run_ffsubsync, video_path, normalized_subtitle_path, aligned_temp_path
        )
        await _append_log(job_id, "ffsubsync", ffsubsync_result)

        await run_in_threadpool(aligned_temp_path.replace, final_subtitle_path)

        files_for_zip: Dict[str, Path] = {f"{video_basename}.srt": final_subtitle_path}
        for extension in (".srt", ".smi"):
            backup = job_dir / f"{video_basename}{extension}.bk"
            if backup.exists():
                files_for_zip[backup.name] = backup

        zip_path: Optional[Path] = None
        if len(files_for_zip) > 1:
            await _update_job(
                job_id,
                message="Packaging results…",
                current_step="Creating ZIP archive",
                progress=0.9,
            )
            zip_path = job_dir / f"{video_basename}_results.zip"
            await run_in_threadpool(_create_zip_archive, files_for_zip, zip_path)

        await _update_job(
            job_id,
            status="completed",
            message="Subtitle synchronized successfully.",
            current_step="Completed",
            progress=1.0,
            final_subtitle_path=final_subtitle_path,
            zip_path=zip_path,
            video_basename=video_basename,
        )
    except PipelineError as exc:
        await _update_job(
            job_id,
            status="failed",
            message="Processing failed.",
            current_step="Failed",
            error_message=str(exc),
        )
        await run_in_threadpool(_safe_cleanup, job_dir)
    except Exception as exc:  # pragma: no cover - unexpected errors
        await _update_job(
            job_id,
            status="failed",
            message="An unexpected error occurred.",
            current_step="Failed",
            error_message=str(exc),
        )
        await run_in_threadpool(_safe_cleanup, job_dir)
        raise
    finally:
        asyncio.create_task(_schedule_job_cleanup(job_id, job_dir))


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

    input_encoding = encoding_override.strip().lower()

    try:
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

        existing_match = job_dir / f"{video_basename}{subtitle_path.suffix.lower()}"
        if not existing_match.exists():
            shutil.copy2(subtitle_path, existing_match)

        logs: Dict[str, CommandResult] = {}
        detected_encoding: Optional[str] = input_encoding or None
        if detected_encoding:
            logs["uchardet"] = CommandResult(
                command=["encoding", "override"],
                stdout=f"User override: {detected_encoding}",
                stderr="",
            )

        job = JobState(
            job_id=job_id,
            created_at=datetime.utcnow(),
            status="queued",
            progress=0.1,
            current_step="Preparing job",
            message="Waiting to start…",
            video_basename=video_basename,
            log_messages=logs,
            detected_encoding=detected_encoding,
        )
        await _register_job(job)

        background_tasks.add_task(
            _run_pipeline,
            job_id,
            job_dir,
            video_path,
            subtitle_path,
            normalized_subtitle_path,
            aligned_temp_path,
            final_subtitle_path,
            video_basename,
            detected_encoding,
        )

        ttl_minutes = max(1, JOB_TTL_SECONDS // 60)
        return templates.TemplateResponse(
            "processing.html",
            {
                "request": request,
                "job_id": job_id,
                "ttl_minutes": ttl_minutes,
            },
            status_code=202,
        )
    except HTTPException:
        _safe_cleanup(job_dir)
        raise
    except PipelineError as exc:
        _safe_cleanup(job_dir)
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "job": JobState(
                    job_id=job_id,
                    created_at=datetime.utcnow(),
                    status="failed",
                    progress=0.0,
                    current_step="Failed",
                    message="Processing failed.",
                    log_messages=logs,
                    error_message=str(exc),
                ),
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


@app.get("/status/{job_id}")
async def job_status(job_id: str) -> Dict[str, object]:
    job = await _get_job(job_id, require_completed=False)
    return {
        "job_id": job.job_id,
        "status": job.status,
        "progress": max(0.0, min(1.0, job.progress)),
        "current_step": job.current_step,
        "message": job.message,
        "detected_encoding": job.detected_encoding,
    }


@app.get("/result/{job_id}", response_class=HTMLResponse)
async def job_result(request: Request, job_id: str) -> HTMLResponse:
    job = await _get_job(job_id, require_completed=False)
    ttl_minutes = max(1, JOB_TTL_SECONDS // 60)

    if job.status == "completed":
        if not job.final_subtitle_path or not job.final_subtitle_path.exists():
            raise HTTPException(status_code=404, detail="Result expired")
        return templates.TemplateResponse(
            "result.html",
            {
                "request": request,
                "job": job,
                "ttl_minutes": ttl_minutes,
            },
        )

    if job.status == "failed":
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "job": job,
                "ttl_minutes": ttl_minutes,
            },
            status_code=500,
        )

    return templates.TemplateResponse(
        "processing.html",
        {
            "request": request,
            "job_id": job_id,
            "ttl_minutes": ttl_minutes,
        },
        status_code=202,
    )


@app.get("/download/{job_id}")
async def download(job_id: str) -> FileResponse:
    job = await _get_job(job_id)
    if not job.final_subtitle_path or not job.final_subtitle_path.exists():
        raise HTTPException(status_code=404, detail="Subtitle file not found")
    return FileResponse(job.final_subtitle_path, filename=f"{job.video_basename}.srt")


@app.get("/download/{job_id}/zip")
async def download_zip(job_id: str) -> FileResponse:
    job = await _get_job(job_id)
    if not job.zip_path or not job.zip_path.exists():
        raise HTTPException(status_code=404, detail="Archive not available")
    return FileResponse(job.zip_path, filename=job.zip_path.name)


async def _get_job(job_id: str, require_completed: bool = True) -> JobState:
    async with registry_lock:
        job = job_registry.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    if require_completed and job.status != "completed":
        raise HTTPException(status_code=409, detail="Job is still processing")
    return job


__all__ = ["app"]

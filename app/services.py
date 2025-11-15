from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

UHC_LABELS = {
    "uhc",
    "cp949",
    "ms949",
    "ms_949",
    "windows-949",
    "949",
    "ks_c_5601-1987",
}


@dataclass
class CommandResult:
    command: Iterable[str]
    stdout: str
    stderr: str


class PipelineError(RuntimeError):
    """Raised when an external tool fails."""


def normalize_encoding_label(raw_label: str | None) -> Optional[str]:
    """Normalize encoding labels returned by uchardet."""
    if not raw_label:
        return None

    normalized = raw_label.strip().lower()
    if not normalized or normalized == "unknown":
        return None

    if normalized in UHC_LABELS:
        return "uhc"

    return normalized


def run_subprocess(command: Iterable[str], cwd: Path) -> CommandResult:
    """Run a subprocess command and capture output."""
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise PipelineError(
            f"Command {' '.join(map(str, command))} failed with exit code {exc.returncode}\n"
            f"STDOUT:\n{exc.stdout}\nSTDERR:\n{exc.stderr}"
        ) from exc

    return CommandResult(command=list(command), stdout=result.stdout, stderr=result.stderr)


def run_uchardet(subtitle_path: Path, cwd: Optional[Path] = None) -> tuple[str, CommandResult]:
    """Detect subtitle encoding using uchardet."""
    working_dir = cwd or subtitle_path.parent
    result = run_subprocess(["uchardet", str(subtitle_path)], cwd=working_dir)
    encoding = normalize_encoding_label(result.stdout)
    if encoding is None:
        raise PipelineError("Unable to detect subtitle encoding with uchardet")
    return encoding, result


def build_pysubs2_command(
    input_path: Path,
    output_path: Path,
    input_encoding: str,
) -> list[str]:
    return [
        "pysubs2",
        "--to",
        "srt",
        "--input-enc",
        input_encoding,
        "--output-enc",
        "utf-8",
        "-o",
        str(output_path),
        str(input_path),
    ]


def run_pysubs2(
    input_path: Path,
    output_path: Path,
    input_encoding: str,
    cwd: Optional[Path] = None,
) -> CommandResult:
    command = build_pysubs2_command(input_path, output_path, input_encoding)
    return run_subprocess(command, cwd or input_path.parent)


def run_ffsubsync(
    video_path: Path,
    subtitle_path: Path,
    output_path: Path,
    cwd: Optional[Path] = None,
) -> CommandResult:
    command = [
        "ffsubsync",
        str(video_path),
        "-i",
        str(subtitle_path),
        "-o",
        str(output_path),
        "--encoding",
        "utf-8",
        "--output-encoding",
        "utf-8",
    ]
    return run_subprocess(command, cwd or video_path.parent)


def cleanup_old_jobs(base_dir: Path, ttl_seconds: int) -> list[Path]:
    """Delete job directories older than the TTL."""
    removed: list[Path] = []
    now = time.time()
    if not base_dir.exists():
        return removed

    for item in base_dir.iterdir():
        try:
            if not item.is_dir():
                continue
            age = now - item.stat().st_mtime
            if age >= ttl_seconds:
                shutil.rmtree(item, ignore_errors=True)
                removed.append(item)
        except FileNotFoundError:
            # Directory might have been removed concurrently.
            continue

    return removed

from __future__ import annotations

import os
import base64
from io import BytesIO
from pathlib import Path
from typing import Iterable

import pytest
from fastapi import UploadFile
from starlette.background import BackgroundTasks
from starlette.requests import Request

from app import main


def _write_script(path: Path, lines: Iterable[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.anyio("asyncio")
async def test_full_pipeline_with_stubbed_cli(tmp_path, monkeypatch):
    scripts_dir = tmp_path / "bin"
    scripts_dir.mkdir()

    uchardet_script = [
        "#!/usr/bin/env python3",
        "import pathlib, sys",
        "if len(sys.argv) < 2:",
        "    sys.stderr.write('missing path\\n')",
        "    sys.exit(1)",
        "pathlib.Path(sys.argv[1]).read_bytes()",
        "sys.stdout.write('utf-8\\n')",
    ]
    _write_script(scripts_dir / "uchardet", uchardet_script)

    pysubs2_script = [
        "#!/usr/bin/env python3",
        "import sys, pathlib",
        "args = sys.argv[1:]",
        "out_index = args.index('-o') + 1",
        "output_path = pathlib.Path(args[out_index])",
        "input_path = pathlib.Path(args[-1])",
        "content = input_path.read_text(encoding='utf-8')",
        "output_path.write_text('NORMALIZED\\n' + content, encoding='utf-8')",
        "sys.stdout.write('normalized\\n')",
    ]
    _write_script(scripts_dir / "pysubs2", pysubs2_script)

    ffsubsync_script = [
        "#!/usr/bin/env python3",
        "import sys, pathlib",
        "args = sys.argv[1:]",
        "video_path = pathlib.Path(args[0])",
        "video_path.read_bytes()",
        "input_path = pathlib.Path(args[args.index('-i') + 1])",
        "output_path = pathlib.Path(args[args.index('-o') + 1])",
        "content = input_path.read_text(encoding='utf-8')",
        "output_path.write_text('ALIGNED\\n' + content, encoding='utf-8')",
        "sys.stdout.write('aligned\\n')",
    ]
    _write_script(scripts_dir / "ffsubsync", ffsubsync_script)

    monkeypatch.setenv("PATH", f"{scripts_dir}{os.pathsep}" + os.environ.get("PATH", ""))

    base_dir = tmp_path / "jobs"
    base_dir.mkdir()
    monkeypatch.setattr(main, "BASE_DIR", base_dir)
    monkeypatch.setattr(main, "JOB_TTL_SECONDS", 60)
    main.job_registry.clear()

    video_base64 = (
        Path("tests/data/sample_mp4_base64.txt").read_text(encoding="ascii").strip()
    )
    video_bytes = base64.b64decode(video_base64)
    subtitle_text = Path("tests/data/sample.srt").read_text(encoding="utf-8")

    video_upload = UploadFile(file=BytesIO(video_bytes), filename="clip.mp4")
    subtitle_upload = UploadFile(file=BytesIO(subtitle_text.encode("utf-8")), filename="clip.srt")

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/process",
        "raw_path": b"/process",
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [],
        "client": ("test", 123),
        "server": ("testserver", 80),
        "app": main.app,
    }

    background_tasks = BackgroundTasks()
    request = Request(scope, receive)

    response = await main.process_request(
        request,
        background_tasks,
        video_upload,
        subtitle_upload,
        encoding_override="",
        force_sami=False,
    )
    assert response.status_code == 202

    await background_tasks()

    job_ids = list(main.job_registry.keys())
    assert job_ids, "Job registry should contain the processed job"
    job_id = job_ids[0]

    status_payload = await main.job_status(job_id)
    assert status_payload["status"] == "completed"
    assert any(log["name"] == "ffsubsync" for log in status_payload["logs"])

    final_path = base_dir / job_id / "clip.srt"
    assert final_path.exists()
    final_content = final_path.read_text(encoding="utf-8")
    assert "ALIGNED" in final_content
    assert "NORMALIZED" in final_content

    zip_path = base_dir / job_id / f"clip_results.zip"
    assert zip_path.exists()

# Subtitle Syncer

Subtitle Syncer is a FastAPI web application that wraps a subtitle-processing pipeline based on
`ffsubsync`, `pysubs2`, and `uchardet`. It lets you upload a video and a subtitle file, detects the
subtitle encoding, optionally converts SAMI subtitles to UTF-8 SRT, synchronizes them against the
video, and returns the aligned subtitle file for download.

## Features

- Upload video (`.mkv`, `.mp4`, `.avi`, `.mov`) and subtitle files (`.smi`, `.srt`, `.ass`, `.ssa`,
  `.sub`, `.vtt`).
- Detect subtitle encoding with `uchardet` or accept a user-provided override.
- Normalize subtitle encoding and format with the `pysubs2` CLI.
- Align subtitles against the reference video via `ffsubsync`.
- Backup existing subtitles that share the video basename and offer a ZIP download with backups.
- Provide detailed processing logs, a live timeline, and incremental progress updates during processing.

## Prerequisites

The application depends on a few external CLI tools that must be available on the host system:

- [`ffsubsync`](https://subsync.readthedocs.io/)
- [`pysubs2`](https://pysubs2.readthedocs.io/en/latest/cli.html)
- [`uchardet`](https://manpages.ubuntu.com/manpages/resolute/man1/uchardet.1.html)

Install them via your distribution packages or `pip` (for `ffsubsync`/`pysubs2`).

## Development environment

This project uses [uv](https://docs.astral.sh/uv/latest/) for environment management. Create the
virtual environment with Python 3.12.12 and install dependencies via:

```bash
uv venv py312 --python 3.12.12

# Activate the environment
# macOS/Linux
source py312/bin/activate
# Windows (PowerShell)
.\py312\Scripts\Activate.ps1
# Windows (Command Prompt)
py312\Scripts\activate.bat

# Install dependencies into the active environment
uv pip install .[dev]
```

Once dependencies are installed you can run the development server with:

```bash
uvicorn app.main:app --reload
```

When you submit files through the form the app redirects to a progress screen that polls the
`/status/{job_id}` endpoint until the job completes. The JSON payload now includes:

- `progress`, `status`, and `current_step` for easy progress bars.
- `events`, a chronological timeline of pipeline milestones that feeds the UI timeline.
- `logs`, an array of collected command outputs that update live on the processing page.

Finished runs are available at `/result/{job_id}`. That page renders the final timeline, log output,
and download buttons. You can also fetch the JSON status directly from `/status/{job_id}` if you want
to build your own UI or integrate with another system.

Open <http://localhost:8000> to access the form. Upload a video and subtitle file to run the
pipeline.

## Testing and quality checks

Run linting, the unit test suite, and coverage reports through uv:

```bash
# Ruff lint checks
./py312/bin/python -m ruff check .

# Unit tests (fast unit + integration suite with stubbed CLI tools)
./py312/bin/python -m pytest

# Coverage (includes running pytest)
./py312/bin/python -m coverage run -m pytest
./py312/bin/python -m coverage report
```

The integration test (`tests/test_integration_pipeline.py`) spins up lightweight Python shims that
emulate the external CLI tools so that CI can exercise the entire FastAPI request pipeline without
requiring heavy native dependencies. If you have the real binaries installed on your machine you can
manually validate them by running the application and uploading sample media—the live timeline and log
panels will stream the subprocess output in real time.

## Project structure

```
app/
  main.py          # FastAPI application and request handling
  services.py      # Helpers for running CLI tools and cleaning job directories
  templates/       # HTML templates for the upload form and result pages
  static/          # Placeholder for static assets (CSS/JS if needed)
tests/             # Unit tests for services and API routes
```

Per-request artifacts live in `/tmp/subsync_jobs` (configurable via the `BASE_DIR` constant). Job
folders are cleaned after roughly one hour.

## Continuous Integration

The repository includes a GitHub Actions workflow that runs Ruff lint checks, pytest-based tests, and
coverage reporting using uv-powered Python 3.12 environments.

## Working with Git branches

This repository currently keeps active development work on the `work` branch. To publish your local
changes so collaborators can fetch them:

```bash
# Commit your local changes
git add <files>
git commit -m "Describe your changes"

# Push to the remote work branch
git push origin work
```

To retrieve the latest code for review on another machine, clone the repository (if needed) and
check out the `work` branch:

```bash
git clone <repository-url>
cd subtitle-syncer
git fetch origin
git checkout work
git pull origin work
```

If the branch already exists locally, `git checkout work` followed by `git pull` is sufficient. You
can verify that your local branch matches the remote by running `git status` and checking for
"up-to-date" output or by using `git status -sb` to view ahead/behind counts.

## License

This project is released under the [MIT License](LICENSE).

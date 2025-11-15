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
- Provide detailed processing logs for debugging.

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
uv venv py312 --python=3.12.12
uv pip install --python ./py312/bin/python .[dev]
```

Activate the environment (optional) and run the development server:

```bash
source py312/bin/activate
uvicorn app.main:app --reload
```

Open <http://localhost:8000> to access the form. Upload a video and subtitle file to run the
pipeline.

## Testing and quality checks

Run linting, the unit test suite, and coverage reports through uv:

```bash
# Ruff lint checks
uv run --python ./py312/bin/python ruff check .

# Unit tests
uv run --python ./py312/bin/python pytest

# Coverage (includes running pytest)
uv run --python ./py312/bin/python coverage run -m pytest
uv run --python ./py312/bin/python coverage report
```

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

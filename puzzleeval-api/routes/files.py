import os
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File

from models.api_models import UploadFilesResponse
from services.run_manager import run_manager

router = APIRouter()

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "uploads"

# Per-file upload cap. The previous code did `await upload_file.read()` with
# no check, so a malicious or accidental multi-GB upload would exhaust server
# memory before any validation fired. Stream-read with a running tally and
# abort cleanly if the cap is exceeded. 100 MiB matches the largest realistic
# user upload (PDF batches, audio session recordings); raise via env var if
# legitimate workflows need more.
MAX_UPLOAD_BYTES_PER_FILE = int(
    os.environ.get("PUZZLEEVAL_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024))
)
# Largest single chunk we read before re-checking the running tally.
_CHUNK_BYTES = 1024 * 1024  # 1 MiB


async def _read_with_cap(upload_file: UploadFile, cap: int) -> bytes:
    """Stream the upload, abort with HTTP 413 if it exceeds the cap.

    Reading in 1 MiB chunks keeps RSS bounded even when the user submits a
    file that's slightly under the cap (vs ``await upload_file.read()`` which
    would materialize the entire body in one go regardless of size).
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload_file.read(_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"File '{upload_file.filename}' exceeds {cap} byte cap "
                    f"(read {total} bytes before aborting). Set "
                    f"PUZZLEEVAL_MAX_UPLOAD_BYTES if you need a higher limit."
                ),
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/runs/{run_id}/files", response_model=UploadFilesResponse)
async def upload_files(run_id: str, files: list[UploadFile] = File(...)):
    state = run_manager.get_run(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Run not found")

    run_upload_dir = UPLOAD_DIR / run_id
    run_upload_dir.mkdir(parents=True, exist_ok=True)

    file_ids = []
    filenames = []

    for upload_file in files:
        file_id = str(uuid.uuid4())[:8]
        safe_name = upload_file.filename or f"file_{file_id}"
        dest = run_upload_dir / safe_name

        content = await _read_with_cap(upload_file, MAX_UPLOAD_BYTES_PER_FILE)
        with open(dest, "wb") as f:
            f.write(content)

        file_ids.append(file_id)
        filenames.append(safe_name)

        state.uploaded_files.append({
            "file_id": file_id,
            "filename": safe_name,
            "path": str(dest),
            "size": len(content),
        })
        state.file_id_to_path[file_id] = str(dest)

    return UploadFilesResponse(file_ids=file_ids, filenames=filenames)

import os
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File

from models.api_models import UploadFilesResponse
from services.run_manager import run_manager

router = APIRouter()

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "uploads"


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

        content = await upload_file.read()
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

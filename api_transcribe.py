import asyncio
import logging
import os
import tempfile
import time
import uuid
from functools import partial
from pathlib import Path
from typing import Literal, Optional

import aiofiles
import aiohttp
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from language_check import detect_russian_drift, format_offset
from srt_polish import is_korotulka_filename
from helpers import (
    transcribe_file,
    create_transcript,
    create_srt_from_json,
    get_google_drive_service,
    get_or_create_shared_drive,
    find_or_create_folder,
    upload_as_google_doc,
    cleanup_temp_file,
)
from ops import require_bearer

router = APIRouter(prefix="/api", tags=["api"])

_jobs: dict[str, dict] = {}
_JOB_TTL_SECONDS = 3600


def _gc_jobs():
    now = time.time()
    expired = [jid for jid, j in _jobs.items() if j.get("finished_at") and now - j["finished_at"] > _JOB_TTL_SECONDS]
    for jid in expired:
        _jobs.pop(jid, None)


class TranscribeRequest(BaseModel):
    file_url: str = Field(..., description="HTTPS URL to download the audio/video file from")
    file_auth: Optional[str] = Field(None, description="Optional Authorization header value (e.g., 'Bearer xoxb-...')")
    filename: str = Field(..., description="Original filename — used for output naming and Slack message text")
    mode: Literal["txt", "srt", "both"] = Field("txt")
    username: str = Field("Anton", description="Drive folder name within Transponster shared drive")
    language_code: Optional[str] = Field(
        None,
        pattern=r"^[a-z]{2,3}$",
        description="Force the STT language (ISO-639 code ElevenLabs understands, e.g. 'ukr'). "
                    "Default None keeps auto-detect — same tradeoff as the 🇺🇦 reaction in chat: "
                    "auto-detect drifts between Ukrainian and Russian on weak audio.",
    )


def artifact_base(filename: str, language_code: Optional[str]) -> str:
    """Output basename. A forced-language rerun MUST NOT reuse the original
    basename: Anton's delivery dedups Slack uploads by filename, so `ep17.txt`
    already in the thread would read as \"delivered\" and the rerun would
    silently re-deliver the OLD transcript. Mirror the chat path's suffix:
    ep17 → ep17-ukr (slack_events.py does the same for the 🇺🇦 reaction)."""
    base = Path(filename).stem
    return f"{base}-{language_code}" if language_code else base


def language_drift_payload(result_data: dict, language_code: Optional[str],
                           filename: str = "") -> Optional[dict]:
    """The API twin of warn_about_russian_drift: same detector, but the consumer
    is a machine (Anton's delivery worker), so the nudge ships as data with a
    ready-to-post note instead of a chat message. Skipped entirely on forced
    runs — the caller already chose the language, nagging would be noise."""
    if language_code:
        return None
    try:
        drift = detect_russian_drift(result_data)
    except Exception:
        logging.exception("[api/transcribe] language drift check failed")
        return None
    if not drift:
        return None
    offset = format_offset(drift["first_start"])
    where = f" (десь від {offset})" if offset else ""
    named = f" файлу `{filename}`" if filename else ""
    return {
        **drift,
        "note": (
            f":eyes: Здається, у розшифровку{named} місцями заїхала російська{where} — так буває, "
            f"коли мова визначається автоматично. Скажи мені — і я перероблю цей файл "
            f"з примусовою українською (це нова розшифровка з аудіо, не переклад)."
        ),
    }


class TranscribeJobCreated(BaseModel):
    job_id: str
    status: str = "queued"
    # Echoed so the client can VERIFY the server honoured the field — an older
    # Transponster silently ignores unknown request fields, and a "forced
    # Ukrainian" receipt that the server never saw would be a lie (Codex).
    language_code: Optional[str] = None


@router.post("/transcribe", response_model=TranscribeJobCreated, status_code=status.HTTP_202_ACCEPTED)
async def transcribe_start(req: TranscribeRequest, _: None = Depends(require_bearer)):
    _gc_jobs()
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "status": "queued",
        "step": "queued",
        "started_at": time.time(),
        "filename": req.filename,
        "mode": req.mode,
        "username": req.username,
        "language_code": req.language_code,
    }
    asyncio.create_task(_run_job(job_id, req))
    logging.info(f"[api/transcribe] queued job {job_id} for {req.filename!r} "
                 f"mode={req.mode} language_code={req.language_code}")
    return TranscribeJobCreated(job_id=job_id, status="queued", language_code=req.language_code)


@router.get("/transcribe/{job_id}")
async def transcribe_status(job_id: str, _: None = Depends(require_bearer)):
    _gc_jobs()
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found or expired")
    return job


async def _run_job(job_id: str, req: TranscribeRequest):
    job = _jobs.get(job_id)
    if not job:
        return
    job["status"] = "running"

    suffix = Path(req.filename).suffix or ""
    fd, temp_path = tempfile.mkstemp(prefix="anton-transcribe-", suffix=suffix)
    os.close(fd)
    temp_file = Path(temp_path)

    try:
        # 1. Download source file
        job["step"] = "downloading"
        headers = {}
        if req.file_auth:
            headers["Authorization"] = req.file_auth
        timeout = aiohttp.ClientTimeout(total=600, sock_connect=30, sock_read=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(req.file_url, headers=headers) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Download failed: HTTP {resp.status}")
                async with aiofiles.open(temp_file, "wb") as f:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        await f.write(chunk)
        size_bytes = temp_file.stat().st_size
        job["downloaded_bytes"] = size_bytes
        logging.info(f"[api/transcribe] {job_id} downloaded {size_bytes} bytes")

        # 2. ElevenLabs transcription (blocking → executor)
        job["step"] = "transcribing"
        loop = asyncio.get_running_loop()
        result_data = await loop.run_in_executor(
            None, partial(transcribe_file, str(temp_file), language_code=req.language_code)
        )

        # 3. Generate outputs
        job["step"] = "generating"
        txt_content: Optional[str] = None
        srt_content: Optional[str] = None
        if req.mode in ("txt", "both"):
            txt_content = create_transcript(result_data)
        if req.mode in ("srt", "both"):
            srt_content = create_srt_from_json(result_data, max_chars=40, max_duration=4.0,
                                               polish=is_korotulka_filename(req.filename))

        # 4. Google Drive upload (only when txt is produced)
        drive_doc_link: Optional[str] = None
        drive_folder_link: Optional[str] = None
        drive_folder_created = False
        base = artifact_base(req.filename, req.language_code)

        if txt_content is not None:
            job["step"] = "drive_upload"
            try:
                drive_service = await loop.run_in_executor(None, get_google_drive_service)
                if drive_service is None:
                    raise RuntimeError("Google Drive service unavailable")
                shared_drive_id = await loop.run_in_executor(None, get_or_create_shared_drive, drive_service)
                if not shared_drive_id:
                    raise RuntimeError("Transponster shared drive not found")
                folder_id, drive_folder_link, created = await loop.run_in_executor(
                    None, find_or_create_folder, drive_service, req.username, shared_drive_id
                )
                drive_folder_created = created
                if folder_id:
                    drive_doc_link = await loop.run_in_executor(
                        None, upload_as_google_doc, drive_service, base, txt_content, folder_id
                    )
            except Exception as e:
                logging.error(f"[api/transcribe] {job_id} Drive upload failed: {e}")

        # 5. Pre-formatted Slack messages (mirror Transponster wording exactly)
        messages: dict[str, str] = {}
        if txt_content is not None:
            messages["txt_initial_comment"] = f":heavy_check_mark: Все вийшло, ось розшифровка для файлу `{req.filename}`."
        if srt_content is not None:
            messages["srt_initial_comment"] = f":heavy_check_mark: Все вийшло, ось субтитри для файлу `{req.filename}`."
        if drive_doc_link and drive_folder_link:
            if drive_folder_created:
                messages["drive_message"] = (
                    f"📂 Я створив для тебе <{drive_folder_link}|папку> у нас на Google Drive "
                    f"і поклав розшифровку туди. <{drive_doc_link}|Ось твоє посилання на файл>."
                )
            else:
                messages["drive_message"] = (
                    f"📂 Цю розшифровку ти також знайдеш в <{drive_folder_link}|оцій папці> "
                    f"як Word документ. <{drive_doc_link}|Ось твоє посилання на файл>."
                )

        job.update({
            "status": "completed",
            "step": "done",
            "finished_at": time.time(),
            "result": {
                "filename_base": base,
                "mode": req.mode,
                "language_code": req.language_code,
                "language_drift": language_drift_payload(result_data, req.language_code, req.filename),
                "txt_content": txt_content,
                "srt_content": srt_content,
                "drive_doc_link": drive_doc_link,
                "drive_folder_link": drive_folder_link,
                "drive_folder_created": drive_folder_created,
                "messages": messages,
            },
        })
        elapsed = job["finished_at"] - job["started_at"]
        logging.info(f"[api/transcribe] {job_id} completed in {elapsed:.1f}s mode={req.mode}")

    except Exception as e:
        logging.exception(f"[api/transcribe] {job_id} failed")
        job.update({
            "status": "failed",
            "error": str(e),
            "finished_at": time.time(),
        })
    finally:
        cleanup_temp_file(str(temp_file))

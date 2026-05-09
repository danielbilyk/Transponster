import hmac
import logging
import os
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from stats import get_stats

router = APIRouter(prefix="/ops", tags=["ops"])

OPS_BEARER_TOKEN = os.getenv("OPS_BEARER_TOKEN", "")
GIT_SHA = os.getenv("GIT_SHA", "unknown")
LOG_FILE = Path("/app/data/bot.log")
TMP_DIR = Path("/tmp")


def require_bearer(request: Request) -> None:
    if not OPS_BEARER_TOKEN:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Ops API disabled (OPS_BEARER_TOKEN not set)")
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    presented = auth[len("Bearer "):].strip()
    if not hmac.compare_digest(presented, OPS_BEARER_TOKEN):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid bearer token")


@router.get("/health")
async def health(_: None = Depends(require_bearer)):
    root = shutil.disk_usage("/")
    data = shutil.disk_usage("/app/data") if Path("/app/data").exists() else None
    return {
        "status": "ok",
        "git_sha": GIT_SHA,
        "git_sha_short": GIT_SHA[:7] if GIT_SHA != "unknown" else "unknown",
        "uptime_seconds": int(time.time() - _start_time),
        "disk_root": _bytes_summary(root),
        "disk_data": _bytes_summary(data) if data else None,
        "log_file_exists": LOG_FILE.exists(),
        "log_file_size_bytes": LOG_FILE.stat().st_size if LOG_FILE.exists() else 0,
    }


@router.get("/disk")
async def disk(_: None = Depends(require_bearer)):
    paths = ["/", "/app", "/app/data", "/tmp"]
    out = {}
    for p in paths:
        if Path(p).exists():
            usage = shutil.disk_usage(p)
            out[p] = _bytes_summary(usage)
    tmp_files = []
    if TMP_DIR.exists():
        for entry in sorted(TMP_DIR.iterdir(), key=lambda e: e.stat().st_mtime if e.exists() else 0, reverse=True)[:20]:
            try:
                st = entry.stat()
                tmp_files.append({
                    "path": str(entry),
                    "size_bytes": st.st_size if entry.is_file() else None,
                    "mtime_age_seconds": int(time.time() - st.st_mtime),
                })
            except FileNotFoundError:
                continue
    return {"disk_usage": out, "tmp_largest_or_recent": tmp_files}


@router.get("/logs")
async def logs(
    n: int = Query(200, ge=1, le=5000),
    _: None = Depends(require_bearer),
):
    if not LOG_FILE.exists():
        return {"lines": [], "note": f"{LOG_FILE} does not exist yet"}
    with LOG_FILE.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        chunk = min(size, max(n * 200, 16_384))
        f.seek(size - chunk)
        tail = f.read().decode("utf-8", errors="replace").splitlines()
    return {"lines": tail[-n:], "log_size_bytes": size}


@router.post("/cleanup-tmp")
async def cleanup_tmp(
    older_than_hours: float = Query(1.0, ge=0.0, le=720.0),
    dry_run: bool = Query(False),
    _: None = Depends(require_bearer),
):
    if not TMP_DIR.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{TMP_DIR} does not exist")
    cutoff = time.time() - (older_than_hours * 3600)
    removed = []
    freed_bytes = 0
    errors = []
    for entry in TMP_DIR.iterdir():
        try:
            st = entry.stat()
            if st.st_mtime >= cutoff:
                continue
            size = _path_size(entry)
            if not dry_run:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
            removed.append({"path": str(entry), "size_bytes": size, "age_seconds": int(time.time() - st.st_mtime)})
            freed_bytes += size
        except (FileNotFoundError, PermissionError) as e:
            errors.append({"path": str(entry), "error": str(e)})
    logging.info(f"[ops] cleanup-tmp: removed={len(removed)} freed={freed_bytes} dry_run={dry_run}")
    return {
        "dry_run": dry_run,
        "older_than_hours": older_than_hours,
        "removed_count": len(removed),
        "freed_bytes": freed_bytes,
        "freed_human": _human_bytes(freed_bytes),
        "removed": removed,
        "errors": errors,
    }


@router.get("/stats")
async def stats(
    year: int | None = Query(None, ge=2024, le=2100),
    _: None = Depends(require_bearer),
):
    return get_stats(year)


def _bytes_summary(usage):
    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_pct": round(usage.used / usage.total * 100, 1) if usage.total else 0,
        "free_human": _human_bytes(usage.free),
        "total_human": _human_bytes(usage.total),
    }


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def _path_size(path: Path) -> int:
    if path.is_file() or path.is_symlink():
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0
    total = 0
    for sub in path.rglob("*"):
        try:
            if sub.is_file():
                total += sub.stat().st_size
        except (FileNotFoundError, PermissionError):
            continue
    return total


_start_time = time.time()

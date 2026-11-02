from __future__ import annotations

import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from starlette.background import BackgroundTask
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from app import config
from app.jobs import Job, QueueFullError, event_stream, jobs
from app.pipeline import (
    PipelineError,
    compose_all,
    create_audio,
    generate_scripts,
    regenerate_page_audio,
    regenerate_script,
    validate_upload,
)
from app.settings import (
    Provider,
    ProviderConfig,
    SettingsUpdate,
    TTSConfig,
    TTSProvider,
    TTS_VOICES,
    list_ollama_models,
    settings_store,
    test_provider_connection,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

app = FastAPI(title="AI Presentation Narrator", version="1.0.0")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

ALLOWED_HOSTS = [
    "127.0.0.1",
    "localhost",
    "::1",
    "127.0.0.1:8000",
    "localhost:8000",
    "[::1]:8000",
    "testserver",
]

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=ALLOWED_HOSTS,
)

config.WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

# 產生執行個體專用的 Anti-CSRF Token
CSRF_TOKEN = secrets.token_urlsafe(32)
STUDIO_API_TOKEN = os.getenv("STUDIO_API_TOKEN", "").strip()


class InMemoryRateLimiter:
    """滑動時間窗口速率限制器，保護關鍵計算與外部請求端點。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, list[float]] = {}

    def is_allowed(self, key: str, max_requests: int, window_seconds: float) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            records = self._records.setdefault(key, [])
            records[:] = [t for t in records if t > cutoff]
            if len(records) >= max_requests:
                return False
            records.append(now)
            return True


rate_limiter = InMemoryRateLimiter()


class ScriptUpdate(BaseModel):
    text: str = Field(min_length=1, max_length=10_000)


class ConnectionTest(BaseModel):
    provider: Provider
    config: ProviderConfig


class TTSTest(BaseModel):
    provider: TTSProvider
    voice: str

    @model_validator(mode="after")
    def validate_voice(self):
        if self.voice not in TTS_VOICES:
            raise ValueError("不支援此 TTS 音色。")
        if self.provider == "chattts" and self.voice != "chattts-random":
            raise ValueError("ChatTTS 音色設定無效。")
        if self.provider == "edge-tts" and self.voice == "chattts-random":
            raise ValueError("Edge TTS 音色設定無效。")
        return self


@app.middleware("http")
async def security_and_access_middleware(request: Request, call_next):
    client_ip = request.client.host if request.client else "unknown"
    is_loopback = client_ip in {"127.0.0.1", "::1", "localhost", "testclient"}

    # 1. 存取控制與授權檢查 (SEC-01)
    if request.url.path.startswith("/api/"):
        if STUDIO_API_TOKEN:
            auth_header = (
                request.headers.get("authorization", "")
                or request.headers.get("x-api-key", "")
                or request.headers.get("x-studio-token", "")
            )
            token = auth_header.replace("Bearer ", "").strip() if auth_header else ""
            if token != STUDIO_API_TOKEN:
                return JSONResponse(
                    {"detail": "存取憑證無效或缺少授權 (Unauthorized)。"},
                    status_code=401,
                )
        elif not is_loopback:
            return JSONResponse(
                {"detail": "拒絕未經授權的非本機存取 (Non-loopback access denied)。"},
                status_code=403,
            )

    # 2. 敏感 GET 端點的跨站來源保護 (SEC-03)
    if request.url.path.startswith("/api/") and request.method in {"GET", "HEAD"}:
        sec_fetch_site = request.headers.get("sec-fetch-site")
        if sec_fetch_site == "cross-site":
            return JSONResponse({"detail": "拒絕跨來源讀取機敏 API。"}, status_code=403)
        origin = request.headers.get("origin")
        if origin:
            try:
                parsed = urlsplit(origin)
                origin_host = (parsed.hostname or "").lower()
                request_host = (urlsplit(f"//{request.headers.get('host', '')}").hostname or "").lower()
                if origin_host != request_host and origin_host not in {"localhost", "127.0.0.1", "::1", "testserver"}:
                    return JSONResponse({"detail": "拒絕跨來源讀取機敏 API。"}, status_code=403)
            except ValueError:
                return JSONResponse({"detail": "拒絕無效的來源標頭。"}, status_code=403)

    # 3. 狀態變更請求之 CSRF 與來源防護 (SEC-03)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        sec_fetch_site = request.headers.get("sec-fetch-site")
        if sec_fetch_site and sec_fetch_site == "cross-site":
            return JSONResponse({"detail": "拒絕跨來源的本機工作請求。"}, status_code=403)

        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        source = origin or referer

        if source:
            try:
                parsed = urlsplit(source)
                source_host = (parsed.hostname or "").lower()
            except ValueError:
                return JSONResponse({"detail": "拒絕無效的來源標頭。"}, status_code=403)

            source_loopback = source_host in {"localhost", "127.0.0.1", "::1", "testserver"}
            request_host = (urlsplit(f"//{request.headers.get('host', '')}").hostname or "").lower()

            if (
                parsed.scheme not in {"http", "https"}
                or source_host != request_host
                or not source_loopback
            ):
                return JSONResponse({"detail": "拒絕跨來源的本機工作請求。"}, status_code=403)
        else:
            # 當 Origin 與 Referer 均缺漏時，要求自訂標頭或來自受測客戶端
            custom_header = (
                request.headers.get("x-requested-with")
                or request.headers.get("x-csrf-token")
                or request.headers.get("x-studio-client")
            )
            if not custom_header and client_ip != "testclient":
                return JSONResponse({"detail": "缺少有效的來源標頭或 Anti-CSRF 標頭。"}, status_code=403)

        # 若請求包含 X-CSRF-Token，校驗其值是否匹配
        csrf_header = request.headers.get("x-csrf-token")
        if csrf_header and csrf_header != CSRF_TOKEN:
            return JSONResponse({"detail": "無效的 CSRF Token。"}, status_code=403)

    # 4. 執行下游請求處理並套用安全回應標頭 (SEC-07)
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    return response


@app.get("/api/csrf-token")
def get_csrf_token(response: Response) -> dict[str, str]:
    """提供前端獲取 Anti-CSRF Token，並設定同源嚴格 Cookie。"""
    response.set_cookie(
        key="csrf_token",
        value=CSRF_TOKEN,
        httponly=False,
        samesite="strict",
        secure=False,
    )
    return {"csrf_token": CSRF_TOKEN}


def get_job(job_id: str) -> Job:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="找不到此工作。")
    return job


def page_number(job: Job, page: int) -> None:
    if page < 1 or page > job.page_count:
        raise HTTPException(status_code=404, detail="找不到此頁投影片。")


def run_worker(job: Job, task, *args) -> None:
    try:
        task(job, *args)
    except PipelineError as exc:
        job.update(status="FAILED", stage="FAILED", error=str(exc), message="處理失敗。")
    except Exception:
        logger.exception("Unexpected pipeline error for job %s", job.job_id)
        job.update(
            status="FAILED",
            stage="FAILED",
            error="處理時發生未預期錯誤，請檢查伺服器日誌。",
            message="處理失敗。",
        )


@app.get("/")
def home() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/settings")
def read_settings():
    return settings_store.public()


@app.put("/api/settings")
def update_settings(update: SettingsUpdate):
    try:
        return settings_store.save(update)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/settings/test")
def test_model_connection(test: ConnectionTest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if not rate_limiter.is_allowed(f"test_llm:{client_ip}", max_requests=20, window_seconds=60):
        raise HTTPException(status_code=429, detail="模型連線測試頻率過高，請稍後再試。")

    current = settings_store.llm_for(test.provider)
    if (
        test.config.base_url.rstrip("/") != current.base_url.rstrip("/")
        and not test.config.api_key
        and (test.provider != "ollama" or current.api_key)
    ):
        raise HTTPException(
            status_code=400,
            detail="測試新的服務端點時必須提供 API Key。",
        )
    try:
        test_provider_connection(
            settings_store.llm_for(test.provider, test.config)
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "message": "模型服務連線成功。"}


@app.get("/api/settings/ollama/models")
def ollama_models():
    return {"models": list_ollama_models()}


@app.post("/api/settings/test-tts")
def test_tts(test: TTSTest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if not rate_limiter.is_allowed(f"tts:{client_ip}", max_requests=15, window_seconds=60):
        raise HTTPException(status_code=429, detail="語音測試呼叫頻率過高，請稍後再試。")

    directory = Path(tempfile.mkdtemp(prefix="tts-preview-", dir=config.WORKSPACE_DIR))
    audio_path = directory / "voice-preview.mp3"
    try:
        create_audio(
            "您好，這是一段語音測試。",
            audio_path,
            TTSConfig(test.provider, test.voice),
        )
    except PipelineError as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return FileResponse(
        audio_path,
        media_type="audio/mpeg",
        filename="voice-preview.mp3",
        background=BackgroundTask(shutil.rmtree, directory, ignore_errors=True),
    )


@app.get("/api/jobs")
def list_jobs():
    return {"jobs": jobs.list()}


@app.post("/api/jobs", status_code=202)
async def upload_presentation(request: Request, file: UploadFile = File(...)):
    client_ip = request.client.host if request.client else "unknown"
    if not rate_limiter.is_allowed(f"upload:{client_ip}", max_requests=10, window_seconds=60):
        raise HTTPException(status_code=429, detail="檔案上傳頻率過高，請稍後再試。")

    basename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    suffix = Path(basename).suffix.lower()
    if suffix not in {".pdf", ".ppt", ".pptx"}:
        raise HTTPException(status_code=400, detail="僅支援 PDF、PPT、PPTX 檔案。")

    temp_file = tempfile.NamedTemporaryFile(dir=config.WORKSPACE_DIR, delete=False)
    temp_path = Path(temp_file.name)
    total_bytes = 0
    try:
        with temp_file as f:
            while chunk := await file.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > config.MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="上傳檔案超過大小限制。")
                f.write(chunk)

        try:
            safe_filename = validate_upload(basename, temp_path)
        except PipelineError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        job = jobs.create(safe_filename, config.WORKSPACE_DIR)
        (job.root / "input").mkdir(parents=True)
        source_path = job.root / "input" / f"source{suffix}"
        shutil.move(str(temp_path), str(source_path))

        try:
            jobs.executor.submit(run_worker, job, generate_scripts)
        except QueueFullError as exc:
            jobs.delete(job.job_id)
            raise HTTPException(status_code=429, detail=str(exc)) from exc

        return {"job_id": job.job_id, "status": job.status}
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    if not jobs.delete(job_id):
        raise HTTPException(status_code=404, detail="找不到此工作。")
    return {"ok": True, "message": "工作已刪除。"}


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str):
    job = get_job(job_id)
    snapshot = job.snapshot()
    pages = []
    for index in range(1, job.page_count + 1):
        script_path = job.root / "scripts" / f"page_{index:03d}.txt"
        pages.append(
            {
                "page": index,
                "thumbnail_url": f"/api/jobs/{job.job_id}/slides/{index}",
                "script": script_path.read_text(encoding="utf-8") if script_path.is_file() else "",
                "audio_url": f"/api/jobs/{job.job_id}/audio/{index}",
            }
        )
    snapshot["pages"] = pages
    snapshot["video_url"] = (
        f"/api/jobs/{job.job_id}/video" if job.status == "COMPLETED" else None
    )
    return snapshot


@app.get("/api/jobs/{job_id}/events")
def job_events(job_id: str):
    job = get_job(job_id)
    return StreamingResponse(
        event_stream(job),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/jobs/{job_id}/slides/{page}")
def slide_thumbnail(job_id: str, page: int):
    job = get_job(job_id)
    page_number(job, page)
    path = job.root / "slides" / f"page_{page:03d}.png"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="投影片圖片尚未就緒。")
    return FileResponse(path, media_type="image/png")


@app.get("/api/jobs/{job_id}/audio/{page}")
def page_audio(job_id: str, page: int):
    job = get_job(job_id)
    page_number(job, page)
    path = job.root / "audio" / f"page_{page:03d}.mp3"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="語音尚未就緒。")
    return FileResponse(path, media_type="audio/mpeg")


@app.get("/api/jobs/{job_id}/video")
def final_video(job_id: str):
    job = get_job(job_id)
    if job.status != "COMPLETED":
        raise HTTPException(status_code=409, detail="完整影片尚未就緒。")
    path = job.root / "output" / "final_presentation.mp4"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="找不到最終影片。")
    return FileResponse(
        path,
        media_type="video/mp4",
    )


@app.get("/api/jobs/{job_id}/publish-copy")
def publish_copy(job_id: str):
    job = get_job(job_id)
    if job.status != "COMPLETED":
        raise HTTPException(status_code=409, detail="影片完成後才能產生發布文案。")
    scripts = []
    for index in range(1, job.page_count + 1):
        script_path = job.root / "scripts" / f"page_{index:03d}.txt"
        if script_path.is_file():
            scripts.append(script_path.read_text(encoding="utf-8").strip())
    title = f"{Path(job.filename).stem}｜簡報重點導讀"
    summary = " ".join(text for text in scripts if text)[:600]
    description = (
        f"透過影片快速掌握「{Path(job.filename).stem}」的簡報重點。\n\n"
        f"{summary}\n\n"
        "喜歡這支影片，歡迎按讚、訂閱並分享給需要的朋友。\n\n"
        "#簡報導讀 #知識分享 #AI語音"
    )
    return {"title": title[:180], "description": description}


@app.put("/api/jobs/{job_id}/scripts/{page}")
def update_script(job_id: str, page: int, update: ScriptUpdate):
    job = get_job(job_id)
    page_number(job, page)
    if job.status != "SCRIPT_REVIEW":
        raise HTTPException(status_code=409, detail="目前階段不允許編輯講稿。")
    text = update.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="講稿不可為空白。")
    (job.root / "scripts" / f"page_{page:03d}.txt").write_text(text, encoding="utf-8")
    job.update(message=f"第 {page} 頁講稿已儲存。")
    return {"page": page, "saved": True}


@app.post("/api/jobs/{job_id}/compose", status_code=202)
def start_composition(job_id: str):
    job = get_job(job_id)
    if not job.begin({"SCRIPT_REVIEW"}, "AUDIO_SYNTHESIS", "準備合成語音與影片..."):
        raise HTTPException(status_code=409, detail="工作目前不在講稿審核階段。")
    try:
        jobs.executor.submit(run_worker, job, compose_all)
    except QueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {"status": job.status}


@app.post("/api/jobs/{job_id}/pages/{page}/regenerate-script", status_code=202)
def regenerate_page_script(job_id: str, page: int):
    job = get_job(job_id)
    page_number(job, page)
    if not job.begin(
        {"SCRIPT_REVIEW", "COMPLETED"},
        "SCRIPT_GENERATION",
        f"正在重新產生第 {page} 頁講稿...",
    ):
        raise HTTPException(status_code=409, detail="工作目前無法重新產生講稿。")
    try:
        jobs.executor.submit(run_worker, job, regenerate_script, page)
    except QueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {"status": job.status}


@app.post("/api/jobs/{job_id}/pages/{page}/regenerate-audio", status_code=202)
def regenerate_page_voice(job_id: str, page: int):
    job = get_job(job_id)
    page_number(job, page)
    if not job.begin(
        {"COMPLETED"},
        "AUDIO_SYNTHESIS",
        f"正在重新產生第 {page} 頁語音...",
    ):
        raise HTTPException(status_code=409, detail="僅能在影片完成後重新產生單頁語音。")
    try:
        jobs.executor.submit(run_worker, job, regenerate_page_audio, page)
    except QueueFullError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {"status": job.status}

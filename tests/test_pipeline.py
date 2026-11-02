import io
import shutil
import uuid
import zipfile

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.jobs import JobManager, jobs
from app.main import app
from app.pipeline import PipelineError, render_pdf, validate_upload


def make_pdf() -> bytes:
    document = pymupdf.open()
    document.new_page(width=320, height=180)
    return document.tobytes()


def make_pptx() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("ppt/presentation.xml", "<presentation/>")
    return output.getvalue()


def test_validate_upload_checks_pdf_content():
    assert validate_upload("deck.pdf", b"%PDF-1.7\n") == "deck.pdf"
    with pytest.raises(PipelineError, match="不是有效的 PDF"):
        validate_upload("deck.pdf", b"not a pdf")


def test_validate_upload_checks_extensions_and_pptx_structure():
    assert validate_upload("deck.pptx", make_pptx()) == "deck.pptx"
    with pytest.raises(PipelineError, match="僅支援"):
        validate_upload("deck.exe", b"anything")
    with pytest.raises(PipelineError, match="不是有效的 PPTX"):
        validate_upload("deck.pptx", b"not a zip")


def test_render_pdf_writes_sequential_slide_images(tmp_path):
    pdf_path = tmp_path / "input.pdf"
    pdf_path.write_bytes(make_pdf())
    paths = render_pdf(pdf_path, tmp_path / "slides")
    assert [path.name for path in paths] == ["page_001.png"]
    assert paths[0].is_file()


def test_job_manager_only_resolves_uuid_jobs(tmp_path):
    manager = JobManager()
    try:
        job = manager.create("deck.pdf", tmp_path)
        assert uuid.UUID(job.job_id)
        assert job.root == tmp_path / job.job_id
        assert manager.get(job.job_id) is job
        assert manager.get("..\\outside") is None
    finally:
        manager.executor.shutdown(wait=True)


def test_upload_rejects_bad_file_without_creating_job():
    client = TestClient(app)
    response = client.post(
        "/api/jobs",
        files={"file": ("bad.pdf", b"not a PDF", "application/pdf")},
    )
    assert response.status_code == 400
    assert "不是有效的 PDF" in response.json()["detail"]


def test_upload_rejects_cross_origin_requests():
    client = TestClient(app)
    response = client.post(
        "/api/jobs",
        headers={"Origin": "https://attacker.example"},
        files={"file": ("bad.pdf", b"not a PDF", "application/pdf")},
    )
    assert response.status_code == 403


def test_homepage_serves_web_interface():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "LLM 服務商與模型自選設定" in response.text
    assert "Google Gemini" in response.text


def test_settings_api_does_not_expose_api_keys():
    response = TestClient(app).get("/api/settings")
    assert response.status_code == 200
    settings = response.json()
    assert settings["provider"] == "ollama"
    assert "api_key" not in settings["providers"]["ollama"]
    assert "api_key_set" in settings["providers"]["ollama"]


def test_tts_test_rejects_unsupported_voice():
    response = TestClient(app).post(
        "/api/settings/test-tts",
        json={"provider": "edge-tts", "voice": "chattts-random"},
    )
    assert response.status_code == 422


def test_completed_job_returns_publish_copy(tmp_path):
    job = jobs.create("quarterly-report.pdf", tmp_path)
    job.status = "COMPLETED"
    job.page_count = 1
    scripts = job.root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "page_001.txt").write_text("本季營運穩健成長。", encoding="utf-8")
    try:
        response = TestClient(app).get(f"/api/jobs/{job.job_id}/publish-copy")
        assert response.status_code == 200
        assert response.json()["title"] == "quarterly-report｜簡報重點導讀"
        assert "本季營運穩健成長" in response.json()["description"]
    finally:
        shutil.rmtree(job.root, ignore_errors=True)


def test_resolve_soffice_respects_env_var(monkeypatch):
    from app.config import _resolve_soffice
    monkeypatch.setenv("SOFFICE_PATH", "custom_soffice_path")
    assert _resolve_soffice() == "custom_soffice_path"


def test_convert_to_pdf_passes_pdf_straight_through(tmp_path):
    from app.pipeline import convert_to_pdf
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.7")
    assert convert_to_pdf(pdf_path) == pdf_path

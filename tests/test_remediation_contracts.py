import io
import shutil
import socket
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import config
from app.jobs import Job, JobManager, jobs
from app.main import app
from app.pipeline import convert_to_pdf
from app.settings import (
    LLMConfig,
    ProviderConfig,
    SettingsStore,
    SettingsUpdate,
    settings_store,
)


# =========================================================================
# Contract 1: Never leak saved API key when base_url changes; 400 if testing
# =========================================================================
def test_contract_1_never_leak_saved_key_on_base_url_change():
    store = SettingsStore()
    # Save a secret key for openai
    openai_default = store.public()["providers"]["openai"]["base_url"]
    providers = {
        name: ProviderConfig(**{**data, "api_key": ""})
        for name, data in store.public()["providers"].items()
    }
    providers["openai"] = ProviderConfig(
        model="gpt-4o-mini",
        base_url=openai_default,
        api_key="mock-test-key-12345",
    )
    store.save(
        SettingsUpdate(
            provider="openai",
            providers=providers,
            tts_provider="chattts",
            voice="chattts-random",
        )
    )

    # 1. When base_url is unchanged and incoming.api_key is empty -> reuse saved key
    same_url_cfg = store.llm_for(
        "openai",
        ProviderConfig(
            model="gpt-4o-mini",
            base_url=openai_default,
            api_key="",
        ),
    )
    assert same_url_cfg.api_key == "mock-test-key-12345"

    # 2. When base_url changed and incoming.api_key is empty -> key is NOT leaked (set to empty)
    diff_url_cfg = store.llm_for(
        "openai",
        ProviderConfig(
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v2",
            api_key="",
        ),
    )
    assert diff_url_cfg.api_key == ""


def test_contract_1_test_connection_rejects_new_base_url_without_key():
    client = TestClient(app)
    # Testing new base_url without providing api_key returns 400
    response = client.post(
        "/api/settings/test",
        json={
            "provider": "openai",
            "config": {
                "model": "gpt-4o-mini",
                "base_url": "https://api.openai.com/v2",
                "api_key": "",
            },
        },
    )
    assert response.status_code == 400
    assert "必須提供 API Key" in response.json()["detail"]


# =========================================================================
# Contract 2: DNS resolution SSRF block & loopback port restriction
# =========================================================================
def test_contract_2_dns_resolution_blocks_private_ip(monkeypatch):
    # Mock socket.getaddrinfo to simulate domain resolving to private IP 10.0.0.1
    def mock_getaddrinfo(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo)
    with pytest.raises(ValidationError, match="私人或保留 IP"):
        ProviderConfig(
            model="test-model",
            base_url="https://evil-internal.example.org",
        )


def test_contract_2_dns_resolution_blocks_loopback_ip_on_domain(monkeypatch):
    # Mock socket.getaddrinfo to simulate domain resolving to 127.0.0.1
    def mock_getaddrinfo(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo)
    with pytest.raises(ValidationError, match="私人或保留 IP"):
        ProviderConfig(
            model="test-model",
            base_url="https://rebind.example.org",
        )


def test_contract_2_unresolvable_domain_rejected(monkeypatch):
    def mock_getaddrinfo(host, port):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo)
    with pytest.raises(ValidationError, match="無法解析服務端點主機名稱"):
        ProviderConfig(
            model="test-model",
            base_url="https://nonexistent-domain-xyz.com",
        )


def test_contract_2_loopback_port_restriction():
    # Trusted port 11434 allowed
    cfg = ProviderConfig(
        model="qwen2.5-vl",
        base_url="http://127.0.0.1:11434",
    )
    assert cfg.base_url == "http://127.0.0.1:11434"

    # Untrusted port 6379 (Redis) rejected
    with pytest.raises(ValidationError, match="受信任的連接埠"):
        ProviderConfig(
            model="qwen2.5-vl",
            base_url="http://127.0.0.1:6379",
        )


# =========================================================================
# Contract 3: TrustedHostMiddleware & protect_local_mutations
# =========================================================================
def test_contract_3_trusted_host_middleware_blocks_untrusted_host():
    client = TestClient(app)
    response = client.get("/", headers={"Host": "attacker.com"})
    assert response.status_code == 400


def test_contract_3_protect_local_mutations_sec_fetch_site():
    client = TestClient(app)
    response = client.post(
        "/api/jobs",
        headers={"Sec-Fetch-Site": "cross-site"},
        files={"file": ("test.pdf", b"%PDF-1.7", "application/pdf")},
    )
    assert response.status_code == 403
    assert "拒絕跨來源" in response.json()["detail"]


def test_contract_3_protect_local_mutations_untrusted_referer():
    client = TestClient(app)
    response = client.post(
        "/api/jobs",
        headers={"Referer": "http://evil.com/page"},
        files={"file": ("test.pdf", b"%PDF-1.7", "application/pdf")},
    )
    assert response.status_code == 403
    assert "拒絕跨來源" in response.json()["detail"]


# =========================================================================
# Contract 4: max_jobs eviction, bounded events, DELETE /api/jobs/{job_id}
# =========================================================================
def test_contract_4_job_events_bounded_to_100(tmp_path):
    job = Job(job_id="test-job", filename="test.pdf", root=tmp_path)
    for i in range(150):
        job.update(message=f"Update {i}")
    assert len(job.events) == 100
    assert job.events[-1]["message"] == "Update 149"


def test_contract_4_max_jobs_eviction(tmp_path):
    manager = JobManager(max_jobs=2)
    j1 = manager.create("job1.pdf", tmp_path)
    j1.status = "COMPLETED"
    j2 = manager.create("job2.pdf", tmp_path)
    j2.status = "COMPLETED"
    assert len(manager._jobs) == 2

    # Creating a 3rd job should evict the oldest completed job (j1)
    j3 = manager.create("job3.pdf", tmp_path)
    assert len(manager._jobs) == 2
    assert manager.get(j1.job_id) is None
    assert manager.get(j2.job_id) is not None
    assert manager.get(j3.job_id) is not None
    manager.executor.shutdown(wait=True)


def test_contract_4_delete_job_endpoint(tmp_path):
    client = TestClient(app)
    job = jobs.create("job-to-delete.pdf", tmp_path)
    job.root.mkdir(parents=True, exist_ok=True)
    job_id = job.job_id
    assert jobs.get(job_id) is not None
    assert job.root.exists()

    # Successful deletion
    resp = client.delete(f"/api/jobs/{job_id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert jobs.get(job_id) is None
    assert not job.root.exists()

    # Deleting nonexistent job returns 404
    resp_404 = client.delete(f"/api/jobs/{job_id}")
    assert resp_404.status_code == 404


# =========================================================================
# Contract 5: Direct file streaming to disk with MAX_UPLOAD_BYTES
# =========================================================================
def test_contract_5_upload_exceeding_max_bytes_returns_413(tmp_path, monkeypatch):
    client = TestClient(app)
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 100)
    response = client.post(
        "/api/jobs",
        files={"file": ("big.pdf", b"%PDF-" + b"A" * 200, "application/pdf")},
    )
    assert response.status_code == 413
    assert "超過大小限制" in response.json()["detail"]


# =========================================================================
# Contract 6: LibreOffice headless security flags
# =========================================================================
def test_contract_6_libreoffice_command_flags(tmp_path, monkeypatch):
    observed_cmd = []

    def fake_run(cmd, **kwargs):
        observed_cmd.extend(cmd)
        pdf_out = Path(cmd[-1]).with_suffix(".pdf")
        pdf_out.write_bytes(b"%PDF-1.7")
        return None

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(config, "SOFFICE", "soffice")
    monkeypatch.setattr(shutil, "which", lambda x: "soffice")

    ppt_path = tmp_path / "deck.pptx"
    ppt_path.write_bytes(b"PK\x03\x04")
    convert_to_pdf(ppt_path)

    assert "--headless" in observed_cmd
    assert "--norestore" in observed_cmd
    assert "--nodefault" in observed_cmd
    assert "--nolockcheck" in observed_cmd
    assert "--nologo" in observed_cmd

import sys
from pathlib import Path

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from package import is_file_allowed, run_security_scan


def test_packager_strictly_excludes_forbidden_files_and_dirs():
    # Workspace files must be excluded
    assert not is_file_allowed(Path("workspace/job-123/source.pptx"))
    assert not is_file_allowed(Path("workspace/job-123/slides/page_001.png"))
    assert not is_file_allowed(Path("workspace/job-123/audio/page_001.mp3"))
    assert not is_file_allowed(Path("workspace/job-123/output/final.mp4"))

    # Asset weights must be excluded
    assert not is_file_allowed(Path("asset/Decoder.safetensors"))
    assert not is_file_allowed(Path("asset/gpt/model.safetensors"))

    # Virtual environment & caches
    assert not is_file_allowed(Path(".venv/Scripts/python.exe"))
    assert not is_file_allowed(Path(".pytest_cache/v/cache"))
    assert not is_file_allowed(Path("app/__pycache__/main.cpython-312.pyc"))

    # Secrets and environment variables
    assert not is_file_allowed(Path(".env"))
    assert not is_file_allowed(Path(".env.production"))
    assert not is_file_allowed(Path("server.key"))
    assert not is_file_allowed(Path("cert.pem"))

    # Allowed files
    assert is_file_allowed(Path(".env.example"))
    assert is_file_allowed(Path("README.md"))
    assert is_file_allowed(Path("requirements.txt"))
    assert is_file_allowed(Path("app/main.py"))
    assert is_file_allowed(Path("app/static/index.html"))
    assert is_file_allowed(Path("tests/test_pipeline.py"))


def test_security_scanner_passes_on_clean_files(tmp_path):
    clean_file = tmp_path / "clean.py"
    clean_file.write_text("def hello():\n    return 'world'\n", encoding="utf-8")
    assert run_security_scan([clean_file.relative_to(tmp_path)]) is True


def test_security_scanner_flags_real_api_key_patterns(tmp_path, monkeypatch):
    import package
    monkeypatch.setattr(package, "ROOT_DIR", tmp_path)
    
    dirty_file = tmp_path / "leak.py"
    # Dynamically generated key pattern to test scanner detection
    fake_key = "sk-" + "9" * 25
    dirty_file.write_text(f"KEY_VAR = '{fake_key}'\n", encoding="utf-8")
    
    assert package.run_security_scan([Path("leak.py")]) is False

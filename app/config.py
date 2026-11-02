import os
import shutil
from pathlib import Path


def _resolve_soffice() -> str:
    env_path = os.getenv("SOFFICE_PATH")
    if env_path:
        return env_path
    if shutil.which("soffice"):
        return "soffice"
    for candidate in [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "LibreOffice" / "program" / "soffice.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "LibreOffice" / "program" / "soffice.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "LibreOffice" / "program" / "soffice.exe",
    ]:
        if candidate.is_file():
            return str(candidate)
    return "soffice"


ROOT_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = Path(os.getenv("PRESENTATION_WORKSPACE", ROOT_DIR / "workspace")).resolve()
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:31b-cloud")
SOFFICE = _resolve_soffice()
FFMPEG = os.getenv("FFMPEG_PATH", "ffmpeg")
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
MAX_SLIDES = int(os.getenv("MAX_SLIDES", "300"))
RENDER_DPI = int(os.getenv("RENDER_DPI", "150"))
MAX_RENDER_PIXELS = 25_000_000

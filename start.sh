#!/usr/bin/env bash
set -e

# 切換至專案根目錄
cd "$(dirname "$0")"

echo "=========================================================="
echo "  AI 簡報導讀影音工作台 - 啟動腳本 (Linux / macOS)"
echo "=========================================================="

# 檢查 Python
if command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_CMD="python"
else
    echo "錯誤：未找到 Python。請先安裝 Python 3.10 以上版本。"
    exit 1
fi

# 建立或啟動虛擬環境
if [ ! -d ".venv" ]; then
    echo "正在建立 Python 虛擬環境 (.venv)..."
    $PYTHON_CMD -m venv .venv
    echo "虛擬環境建立完成。"
fi

# 載入虛擬環境
source .venv/bin/activate

# 檢查基本相依套件
python -c "import fastapi, uvicorn, multipart, pymupdf, httpx, numpy" 2>/dev/null || {
    echo "檢測到缺少必要套件，正在自 requirements.txt 安裝..."
    pip install -r requirements.txt
}

# 檢查外部工具
command -v ffmpeg >/dev/null 2>&1 || echo "警告：未找到 FFmpeg。影片多軌合成需要 FFmpeg。"
command -v soffice >/dev/null 2>&1 || echo "提示：未找到 LibreOffice (soffice)。PPT/PPTX 轉檔需要 LibreOffice，PDF 檔案原生支援。"

# 檢查 Ollama
export OLLAMA_MODEL="${OLLAMA_MODEL:-gemma4:31b-cloud}"
if command -v ollama >/dev/null 2>&1; then
    if ! curl -s --fail http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
        echo "正在啟動本機 Ollama 服務..."
        ollama serve >/dev/null 2>&1 &
        sleep 2
    fi
fi

APP_HOST="${APP_HOST:-127.0.0.1}"
APP_PORT="${APP_PORT:-8000}"

echo "即將於 http://${APP_HOST}:${APP_PORT} 啟動服務..."
echo "按 Ctrl+C 可停止服務。"
echo "=========================================================="

python -m uvicorn app.main:app --host "$APP_HOST" --port "$APP_PORT"


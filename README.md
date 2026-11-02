# AI 簡報導讀影片

將 PDF/PPT/PPTX 轉成逐頁繁體中文講稿、語音與 MP4 導讀影片。支援 Ollama、Google Gemini、NVIDIA NIM、OpenAI 相容視覺模型；語音可選本機 ChatTTS 或 Edge TTS。

## 需求

- Python 3.10+
- Ollama，並下載支援圖片的視覺模型，例如 `qwen2.5-vl`
- FFmpeg（需包含 `libx264` 與 `libmp3lame`）
- LibreOffice（只有處理 `.ppt` / `.pptx` 時需要）
- ChatTTS 與其相依的 PyTorch。依作業系統及 GPU，安裝方式可能不同，請依 [ChatTTS 官方安裝說明](https://github.com/2noise/ChatTTS) 安裝，再安裝本專案依賴
- 使用 Edge TTS 時，需要網路連線；講稿文字會傳送至 Microsoft 語音服務

## 安裝與啟動

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:OLLAMA_MODEL = "qwen2.5-vl"
ollama pull qwen2.5-vl
```

依作業系統及 GPU 安裝相容的 ChatTTS / PyTorch（可參考 `requirements-ai.txt` 與官方安裝指引）。如使用 Ollama，在第一個終端機啟動服務：

```powershell
$env:OLLAMA_MODEL = "qwen2.5-vl"
ollama serve
```

另開一個終端機啟動網站：

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

瀏覽 `http://127.0.0.1:8000`，上傳簡報後等待講稿生成，審核或編輯講稿，再開始合成影片。影片完成後可播放、下載、重新產生單頁講稿或語音，也可在「影片庫與發布文案」產生並複製影片標題與說明。

到「AI 模型與服務商」頁面可選擇模型供應商，設定模型名稱、服務端點與 API Key，並先測試連線。Ollama 必須選擇支援圖片輸入的視覺模型；模型清單會顯示目前已安裝的模型。API Key 僅存在網站執行期間的伺服器記憶體；服務重啟後需重新輸入。雲端模型會收到投影片圖片，切換至 Edge TTS 則講稿會傳送至 Microsoft 語音服務。請勿將機密內容傳送至未核准的外部服務。

Windows 使用者也可以直接雙擊 [start.bat](./start.bat) 啟動。啟動器會使用專案 `.venv`、嘗試啟動 Ollama，並開啟瀏覽器；它不會自動下載大型 Ollama 模型。

第一次使用 ChatTTS 時可能會下載模型檔並占用大量記憶體或 GPU。模型載入、語音推論及影片合成均在本機執行。

## 設定

可用環境變數：

| 變數 | 預設值 | 說明 |
|---|---|---|
| `OLLAMA_URL` | `http://127.0.0.1:11434` | 本機 Ollama URL |
| `OLLAMA_MODEL` | `qwen2.5-vl` | 視覺模型名稱 |
| `PRESENTATION_WORKSPACE` | 專案下的 `workspace` | 工作檔案目錄 |
| `MAX_UPLOAD_BYTES` | `52428800` | 上傳大小上限 |
| `MAX_SLIDES` | `300` | 最大頁數 |
| `RENDER_DPI` | `150` | PDF 轉圖 DPI |
| `SOFFICE_PATH` | `soffice` | LibreOffice 執行檔路徑 |
| `FFMPEG_PATH` | `ffmpeg` | FFmpeg 執行檔路徑 |

網站預設只監聽 `127.0.0.1`，且未提供使用者登入或多租戶隔離。不要直接暴露到公網或不受信任的網路。上傳檔案、逐頁圖片、講稿、語音和影片會保存在工作目錄；請依資料敏感度管理該目錄及備份。

工作狀態目前保存在記憶體中；重新啟動網站後，先前工作無法從介面恢復，但工作檔案仍會留在 workspace。

## 驗證

```powershell
python -m pytest
```

測試不會下載或載入 Ollama、ChatTTS 模型。

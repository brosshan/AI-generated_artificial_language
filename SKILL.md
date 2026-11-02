---
name: ai-presentation-narrator
description: 自動將 PDF、PPT、PPTX 簡報轉為逐頁高解析度影像、調用 Vision LLM 視覺多模態模型撰寫口語導讀講稿、透過 TTS 引擎合成旁白語音，並以 FFmpeg 壓製逐頁 MP4 拼接為完整高畫質導讀影片。
---

# Skill: AI 簡報導讀影片製作 (AI Presentation Narrator)

本技能提供將簡報檔案（PDF、PPT、PPTX）全自動或人機協同轉換為專業影音導讀影片的標準化作業程序（SOP）。

---

## 1. 架構與技術棧

| 處理階段 | 核心技術 / 套件 | 功用與規格 |
| :--- | :--- | :--- |
| **文件解析與轉圖** | `PyMuPDF (fitz)` / `LibreOffice (soffice)` | 支援 `.pdf`, `.pptx`, `.ppt`；自動調用 LibreOffice 轉 PDF，並渲染為 150~300 DPI 高解析度 PNG 影像。 |
| **視覺模型撰稿** | `Ollama` / `Google Gemini` / `NVIDIA NIM` / `OpenAI` | 逐頁傳入投影片圖片與提示詞（Prompt），由視覺大模型以「大白話」風格產出繁體中文口播講稿。 |
| **文字轉語音 (TTS)** | `ChatTTS` (本機神經語音) / `Edge TTS` (微軟雲端語音) | 支援本機離線運算與微軟高品質台灣華語（曉臻、曉雨、雲哲），輸出標準 44.1kHz / 128kbps MP3。 |
| **影音多軌合成** | `FFmpeg` (subprocess CLI) | 單頁圖片與音訊循環組裝（`-loop 1 -tune stillimage`），並利用 Concat Demuxer 達成無損拼接輸出 MP4。 |
| **工作台與即時推播** | `FastAPI`, `Server-Sent Events (SSE)` | 提供現代化暗黑模式介面、即時進度條、講稿線上審查編輯與發布文案一鍵生成。 |

---

## 2. 檔案目錄規範

每次製作任務皆隔離於獨立的工作目錄中，避免多工作資源競爭與衝突：

```text
workspace/{job_id}/
├── input/                  # 原始簡報來源 (source.pdf 或 source.pptx)
├── libreoffice-profile/    # LibreOffice 轉換時獨立隔離之使用者設定檔
├── slides/                 # 逐頁投影片圖片 (page_001.png, page_002.png, ...)
├── scripts/                # 逐頁導讀講稿 (page_001.txt, page_002.txt, ...)
├── audio/                  # 逐頁合成語音 (page_001.mp3, page_002.mp3, ...)
├── clips/                  # 逐頁單段影片 (page_001.mp4, page_002.mp4, ...)
└── output/                 # 最終合成影片 (final_presentation.mp4)
```

---

## 3. 六大核心執行步驟 (Pipeline Workflow)

### 步驟 1：簡報驗證與安全上傳 (File Ingestion)
1. 驗證副檔名白名單（僅允許 `.pdf`, `.ppt`, `.pptx`）。
2. 進行 ZIP 炸彈防護與魔術位元組（Magic Bytes）頭部檢查，防止惡意代碼偽裝。
3. 為每個任務產生安全 UUID 工作識別碼。

### 步驟 2：投影片拆解高畫質圖檔 (Slide Rendering)
- **PPT / PPTX 轉檔**：
  自動偵測系統 LibreOffice 路徑（支援 Windows 預設 `C:\Program Files\LibreOffice\program\soffice.exe` 或自訂 `SOFFICE_PATH`）：
  ```bash
  soffice --headless -env:UserInstallation=file:///path/to/profile --convert-to pdf source.pptx --outdir workspace/{job_id}/input/
  ```
- **PDF 拆圖邏輯 (PyMuPDF)**：
  ```python
  import pymupdf

  def render_pdf_to_images(pdf_path, output_dir, dpi=150):
      doc = pymupdf.open(pdf_path)
      paths = []
      for idx, page in enumerate(doc, start=1):
          pix = page.get_pixmap(dpi=dpi)
          img_path = output_dir / f"page_{idx:03d}.png"
          pix.save(img_path)
          paths.append(img_path)
      return paths
  ```

### 步驟 3：多模態視覺撰寫口語講稿 (Vision-to-Script)
- 將單頁圖片編碼為 Base64 或本地路徑，傳遞至視覺語言模型（VLM）。
- **內建繁體中文講師 System Prompt**：
  ```text
  你是一位專業且說話生動、擅長用「大白話」講解簡報的工程師講師。
  請閱讀這張投影片，為這張投影片撰寫一段繁體中文口語導讀講稿（約 60-120 字）。
  直接輸出講稿文字，不要包含任何「大家好」、「第幾頁」等格式詞。
  ```
- **多模型相容**：
  - 本機 Ollama：`http://127.0.0.1:11434/api/generate`（支援 `gemma4:31b-cloud`、`qwen2.5-vl` 等視覺模型）。
  - 雲端模型：Google Gemini、NVIDIA NIM、OpenAI GPT-4o-mini 等標準端點。

### 步驟 4：講稿編審與微調 (Human-in-the-loop Review)
- 管線進入 `SCRIPT_REVIEW` 暫停階段。
- 使用者可在 Web 介面即時對照左側投影片圖片與右側文字框：
  - 允許手動潤飾說詞或修正專有名詞。
  - 支援「重新產生此頁講稿」，單頁重新請求視覺模型。
- 使用者確認滿意後，點擊「確認講稿並開始合成」觸發後續步驟。

### 步驟 5：文字轉語音合成 (TTS to MP3)
- 遍歷所有 `page_{idx}.txt` 生成對應的 `page_{idx}.mp3`。
- **微軟 Edge TTS**：
  ```bash
  edge-tts --voice zh-TW-HsiaoChenNeural --text "講稿內容" --write-media page_001.mp3
  ```
- **本機 ChatTTS**：本地神經網路直接合成無外網傳輸風險之語音。

### 步驟 6：多軌音畫合成與最終無損拼接 (FFmpeg Pipeline)
1. **單頁圖音合成短片**：
   ```bash
   ffmpeg -y -loop 1 -framerate 30 -i page_001.png -i page_001.mp3 -codec:v libx264 -tune stillimage -pix_fmt yuv420p -codec:a aac -b:a 192k -shortest page_001.mp4
   ```
2. **無損拼接為完整演講影片**：
   ```bash
   ffmpeg -y -f concat -safe 0 -i concat_list.txt -codec copy final_presentation.mp4
   ```

---

## 4. 環境變數配置

| 變數名稱 | 預設值 | 說明 |
| :--- | :--- | :--- |
| `PRESENTATION_WORKSPACE` | `workspace/` | 任務工作目錄路徑 |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | 本機 Ollama API 服務端點 |
| `OLLAMA_MODEL` | `gemma4:31b-cloud` | 預設視覺模型（若未安裝會自動偵測已下載之模型） |
| `SOFFICE_PATH` | `soffice` | LibreOffice 執行檔路徑（支援自動偵測 Windows 安裝目錄） |
| `FFMPEG_PATH` | `ffmpeg` | FFmpeg 執行檔路徑 |
| `RENDER_DPI` | `150` | 簡報轉圖解析度 DPI |
| `STUDIO_API_TOKEN` | *(選填)* | 若設定此變數，所有 API 強制驗證 Bearer Token |

---

## 5. 常見問題與錯誤復原

1. **找不到 LibreOffice soffice**：
   - 確保已安裝 LibreOffice，Windows 預設路徑 `C:\Program Files\LibreOffice\program\soffice.exe` 會被程式自動載入。
   - 若手動另存為 `.pdf` 格式上傳，則完全不需要安裝 LibreOffice。
2. **模型服務回應 404 (Model Not Found)**：
   - 請檢查 Ollama 是否已下載該模型（`ollama list`）。
   - 請至設定頁面選擇支援圖片輸入（Vision）的模型，如 `gemma4:31b-cloud` 或 `qwen2.5-vl`。
3. **音訊轉換超時**：
   - 系統已內建每頁 180 秒安全超時與行程隔離，並支援單頁重製（Regenerate Audio）。


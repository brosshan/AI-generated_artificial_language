# Skill: AI Presentation Narrator & Video Generator

## 1. 技能概述
本技能負責將簡報投影片（PDF / PPT / PPTX）進行自動化管線處理：解析投影片為循序高畫質影像、調用本地端 Ollama 視覺多模態模型生成逐頁導讀講稿、透過 TTS 引擎轉成語音、將單頁圖音合成單頁 MP4，最後拼接為完整導讀影片。

---

## 2. 核心技術棧與套件依賴

- **文件轉換與圖檔解析**
  - `PyMuPDF` (`fitz`): 處理 PDF 轉高解析度 PNG（推薦解析度 DPI 150~300）。
  - `LibreOffice (soffice CLI)` / `python-pptx`: 處理 PPT/PPTX 轉 PDF。
  - `Pillow` (PIL): 影像尺寸與格式標準化。
- **本地端多模態 AI (Vision LLM)**
  - `Ollama API`: 本地端視覺模型（如 `llama3.2-vision`、`qwen2.5-vl` 或 `llava`）。
  - 任務：傳入單頁投影片 Base64/路徑影像 + 結構化 Prompt，輸出自然口語化解說腳本。
- **文字轉語音 (TTS)**
  - `edge-tts` (微軟輕量自然語音，支援繁體中文) 或本地端 `ChatTTS` / `CosyVoice`。
  - 產出格式：44.1kHz / 128kbps MP3。
- **音訊與影片處理 (FFmpeg)**
  - `ffmpeg-python` 或原生 `subprocess.run(["ffmpeg", ...])`。
  - 單頁圖音合成：使用靜態圖檔循環 (`-loop 1`) + 音訊長度對齊 (`-tune stillimage`, `-c:v libx264`, `-c:a aac`)。
  - 影片拼接：使用 FFmpeg `concat demuxer` 達成無損快速合併。

---

## 3. 工作目錄規範

專案需採用獨立工作區隔離每次任務，目錄格式如下：

```text
workspace/{job_id}/
├── input/          # 原始上傳簡報 (presentation.pdf 或 presentation.pptx)
├── slides/         # 拆解後的影像 (page_001.png, page_002.png, ...)
├── scripts/        # 產出的文字稿 (page_001.txt, page_002.txt, ...)
├── audio/          # 語音檔 (page_001.mp3, page_002.mp3, ...)
├── clips/          # 單頁短影片 (page_001.mp4, page_002.mp4, ...)
└── output/         # 最終影片 (final_presentation.mp4)
```

---

## 4. 六大步驟標準操作規範

### Step 1: 檔案匯入 (File Ingestion)
- 支援副檔名：`.pdf`, `.ppt`, `.pptx`。
- 上傳後檢驗檔案完整度並自動產生唯一的 `job_id`（如 `job_YYYYMMDD_HHMMSS`）。

### Step 2: 講義拆解圖檔 (Slides to Images)
- **PDF 拆解邏輯（Python / PyMuPDF）**：
  ```python
  import fitz  # PyMuPDF

  def convert_pdf_to_images(pdf_path: str, output_dir: str, dpi: int = 200):
      doc = fitz.open(pdf_path)
      image_paths = []
      for page_index in range(len(doc)):
          page = doc.load_page(page_index)
          pix = page.get_pixmap(dpi=dpi)
          output_path = f"{output_dir}/page_{page_index + 1:03d}.png"
          pix.save(output_path)
          image_paths.append(output_path)
      return image_paths
  ```
- **PPT / PPTX 轉檔**：
  若為 PPT/PPTX，先以 headless 模式轉成 PDF 再拆圖：
  ```bash
  soffice --headless --convert-to pdf presentation.pptx --outdir workspace/{job_id}/input/
  ```

### Step 3: 本地 Ollama 產生逐頁文字腳本 (Vision-to-Script)
- **API 端點**：`http://localhost:11434/api/generate` 或使用官方 `ollama` Python SDK。
- **System Prompt 規範**：
  ```text
  你是一位專業且生動的簡報講師。請根據提供的這張投影片圖片內容，撰寫一段 30 秒至 1 分鐘的繁體中文口語解說稿。
  
  要求：
  1. 口吻親切、自然且口語化，適合直接朗讀給聽眾聽。
  2. 請勿輸出「如投影片所示」、「大家可以看到螢幕」等無意義贅詞，直接講述重點。
  3. 請直接輸出純台詞文字，不要附加任何標題、前言、後記或 Markdown 格式。
  ```
- **Python 範例**：
  ```python
  import ollama

  def generate_script(image_path: str, prompt: str) -> str:
      response = ollama.generate(
          model='llama3.2-vision',
          prompt=prompt,
          images=[image_path]
      )
      return response['response'].strip()
  ```

### Step 4: 口語檔案生成 (TTS to MP3)
- 針對 `scripts/page_XXX.txt` 依序生成 `audio/page_XXX.mp3`。
- **使用 edge-tts 指令**（台灣微軟自然語音）：
  ```bash
  edge-tts --voice zh-TW-HsiaoChenNeural --text-file workspace/{job_id}/scripts/page_001.txt --write-media workspace/{job_id}/audio/page_001.mp3
  ```

### Step 5: 合併單頁圖檔與 MP3 (Image + Audio -> Single MP4)
- 依據音訊長度產生固定解析度（1920x1080）之短片，使用 `pad` 濾鏡確保直橫式投影片皆能正確居中且不失真：
  ```bash
  ffmpeg -y -loop 1 -i workspace/{job_id}/slides/page_001.png -i workspace/{job_id}/audio/page_001.mp3 \
    -c:v libx264 -tune stillimage -c:a aac -b:a 192k -pix_fmt yuv420p \
    -vf "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black" \
    -shortest workspace/{job_id}/clips/page_001.mp4
  ```

### Step 6: 循序合併所有單頁短片 (Final Video Concatenation)
1. 自動生成循序清單檔 `concat_list.txt`：
   ```text
   file 'workspace/{job_id}/clips/page_001.mp4'
   file 'workspace/{job_id}/clips/page_002.mp4'
   file 'workspace/{job_id}/clips/page_003.mp4'
   ```
2. 執行 FFmpeg Concat 合併：
   ```bash
   ffmpeg -y -f concat -safe 0 -i workspace/{job_id}/concat_list.txt -c copy workspace/{job_id}/output/final_presentation.mp4
   ```

---

## 5. 異常處理與防呆機制
1. **空白或無效文字保護**：若模型產出空字串或少於 5 個字，自動填入備用轉場詞（如：「我們接著看下一張重點。」）。
2. **GPU 顯存控制**：處理 30 頁以上投影片時，Ollama 請求需循序執行或限制並行數（Worker Concurrency = 1），避免本地端 GPU Out of Memory (OOM)。
3. **音畫同步檢查**：驗證產出的單頁 MP4 時長是否與 MP3 音檔時長相差在 0.5 秒內。
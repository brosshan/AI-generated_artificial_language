# Agent: Presentation Video Production Workflow Orchestrator

## 1. 代理人架構與角色定位
- **Agent 名稱**：`PresentationVideoOrchestrator`
- **主要職責**：管理與協調 6 大處理階段的狀態機（Finite State Machine, FSM）、控管批次任務進度、支援人機協同審核（Human-in-the-loop），並向前端 Web 介面推播即時進度。

---

## 2. 狀態機工作流 (State Machine Pipeline)

```
[IDLE] 
  │ (使用者上傳 PDF / PPT)
  ▼
[STAGE 1: DOCUMENT_PARSING] ──── 解析投影片並產出循序圖片 (page_001 ~ page_NNN)
  │
  ▼
[STAGE 2: SCRIPT_GENERATION] ─── 本地端 Ollama 多模態批次生成口語講稿
  │
  ├─► [INTERRUPT: SCRIPT_REVIEW] (可選：在 Web 介面提供使用者確認/微調講稿)
  │
  ▼
[STAGE 3: AUDIO_SYNTHESIS] ───── 批次呼叫 TTS 產出逐頁 MP3
  │
  ▼
[STAGE 4: CLIP_COMPOSITION] ──── FFmpeg 合成每頁獨立的靜態圖音 MP4
  │
  ▼
[STAGE 5: FINAL_CONCAT] ──────── 拼接所有短片成單一完整演講影片
  │
  ▼
[COMPLETED] ──────────────────── 產出影片下載與線上播放連結
```

---

## 3. 子代理人與模組職責

### 3.1 Document Extraction Worker
- **觸發條件**：收到檔案上傳。
- **職責**：
  1. 檔案格式驗證（PDF/PPT/PPTX）。
  2. 若為 PPT/PPTX，調用轉換服務將其轉為 PDF。
  3. 依序提取為高解析度圖檔至 `slides/` 目錄。
- **回傳成果**：總頁數 $N$ 與圖檔路徑陣列。

### 3.2 Vision Script Agent (Ollama Interface)
- **觸發條件**：圖片解碼完成。
- **職責**：
  1. 讀取每一頁投影片圖片。
  2. 呼叫本地 Ollama Vision API（單頁依序處理，避免顯存溢出）。
  3. 清理模型產出的特殊符號與無效前綴，輸出純台詞寫入 `scripts/`。
- **回傳成果**：逐頁講稿清單，並觸發 Web 介面編輯預覽。

### 3.3 Audio Voice Agent
- **觸發條件**：講稿確認（自動或人工確認）。
- **職責**：
  1. 遍歷講稿檔案，調用 TTS 生成音訊。
  2. 校驗音訊檔案大小及長度有效性。
- **回傳成果**：MP3 音訊檔案清單。

### 3.4 Media Assembly Agent (FFmpeg Engine)
- **觸發條件**：所有圖片與音訊就緒。
- **職責**：
  1. 單頁合成：將 `page_{i}.png` 與 `page_{i}.mp3` 組裝成 `page_{i}.mp4`。
  2. 循序合併：組裝清單並以無損 `concat` 模式合成最終的 `final_presentation.mp4`。
- **回傳成果**：最終影片路徑與時長資訊。

---

## 4. Web API 與前後端通訊協議規格

建議使用 WebSocket 或 Server-Sent Events (SSE) 推播給前端進度條與狀態：

### 進度推播事件範例 (JSON Payload)
```json
{
  "job_id": "job_20261101_091500",
  "status": "PROCESSING",
  "stage": "SCRIPT_GENERATION",
  "current_step": 5,
  "total_steps": 30,
  "progress_percentage": 16.6,
  "message": "正在使用 Ollama 產生第 5/30 頁口語講稿...",
  "payload": {
    "page": 5,
    "thumbnail_url": "/api/jobs/job_20261101_091500/slides/page_005.png",
    "script_preview": "接下來請大家看到第五頁，這是我們系統的核心模組流程..."
  }
}
```

---

## 5. 網頁化人機協同功能（推薦實作）

1. **講稿線上編輯器 (Script Editor)**：
   在 `STAGE 2` 結束後自動暫停管線，讓使用者能在網頁表格中同時對照「左側投影片圖片」與「右側文字講稿」，允許手動修改說詞後再按下「開始合成影片」。
2. **單頁重製 (Single Page Regeneration)**：
   若某頁辨識不佳或聲音發音需微調，支援單獨針對第 $K$ 頁重新呼叫 Ollama 或重新生成音訊，避免整份簡報需從頭重新跑過。
# 資安檢視摘要

## 檢視範圍

檢視文件：

- [ai_presentation_narrator_skill.md](./ai_presentation_narrator_skill.md)
- [ai_presentation_orchestrator_agent.md](./ai_presentation_orchestrator_agent.md)

本次僅檢視 Markdown 規格，未檢視實際 API、程式碼、授權邏輯或部署設定。因此，以下是規格層級的風險，不能視為已確認的可利用漏洞。

後續已依規格建立本機網頁版，提供 Ollama、Gemini、NVIDIA NIM、OpenAI 相容模型，以及本機 ChatTTS / Edge TTS 選項。API Key 僅在伺服器記憶體保存，並對變更資料的請求檢查瀏覽器來源。這些實作不代表完成完整程式碼安全稽核；未提供登入及多租戶隔離，仍不應對外公開。

## 功能摘要

此專案規劃將 PDF/PPT/PPTX 簡報轉成逐頁圖片，呼叫本機 Ollama 視覺模型產生繁體中文講稿，使用 TTS 生成語音，再以 FFmpeg 合成逐頁 MP4 並串接為完整影片。Orchestrator 文件補充狀態機、各階段工作者分工、前端進度推播、講稿人工審核及單頁重新生成流程。

## 發現

未發現可由規格確認、已有明確利用路徑的漏洞。檢視時發現的規格風險及目前狀態如下：

| # | Severity | File | Lines | Vulnerability | Confidence |
|---|----------|------|-------|---------------|------------|
| 1 | 🟡 MEDIUM | [ai_presentation_narrator_skill.md](./ai_presentation_narrator_skill.md) | 17-18, 96-100 | Edge TTS 模式會將講稿文字傳送到 Microsoft 語音服務；雲端 LLM 模式也會將投影片圖片傳至服務供應商。本次實作在介面提醒資料外傳，但使用者仍需確認簡報內容適合送出。 | 8/10 |
| 2 | 🟡 MEDIUM | [ai_presentation_narrator_skill.md](./ai_presentation_narrator_skill.md), [ai_presentation_orchestrator_agent.md](./ai_presentation_orchestrator_agent.md) | 47; 77-87 | 規格未定義使用者驗證及工作所有權。本次實作使用隨機 UUID、預設 loopback 綁定及變更請求來源檢查，降低未授權跨來源存取風險；但沒有登入或多租戶控制，不適合公開部署。API Key 僅在記憶體保存，重啟後需重新設定。 | 8/10 |

## 建議

- 僅在本機執行網站；若需多人或網路部署，先加入登入、工作所有權檢查及資源存取控制。
- 在程式碼及部署設定可供檢視後，再進行完整安全檢查；本文件不是該程式碼的完整安全稽核。

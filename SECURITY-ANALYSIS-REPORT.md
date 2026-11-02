# 資訊安全分析與弱點審查報告 (Security Analysis Report)

**專案名稱**：AI 簡報導讀影音工作台 (AI Presentation Narrator Studio)  
**審查日期**：2026-11-01  
**報告版本**：1.0.0  
**目標路徑**：`<project-root>`  
**語言規範**：繁體中文 (Traditional Chinese)

---

## 1. 執行摘要 (Executive Summary)

本次資安審查針對本機 Python AI 語音與簡報導讀專案之原始碼、設定檔、相依套件與架構實作進行全面檢測。檢測範圍包含 `app/` 核心模組（`main.py`, `settings.py`, `pipeline.py`, `jobs.py`, `config.py`）、前端介面（`app/static/index.html`）、啟動程序（`start.bat`）、測試套件（`tests/`）以及相依管理清單（`requirements.txt`, `requirements-ai.txt`）。

審查發現該專案已實作多項良好的基礎防禦措施，例如：
- 採用 UUID 作為工作識別碼，避免順序性工作 ID 遍歷。
- 投影片頁碼與檔案副檔名進行了強制型別與副檔名白名單驗證，阻斷了典型的路徑遍歷（Path Traversal）漏洞。
- 外部指令執行（FFmpeg、LibreOffice）均使用參數陣列搭配 `shell=False`，有效防範命令注入（Command Injection）。
- 實作了 `TrustedHostMiddleware` 與變更請求的來源檢查（`protect_local_mutations`）。
- API 金鑰僅保存在記憶體中，且在變更端點時具備防外洩邏輯。

**然而，系統仍存在數個嚴重性較高的結構性弱點**：
1. **完全缺乏身分驗證與授權機制**：所有端點皆未實作任何認證（Authentication）或多租戶存取控制（Access Control）。
2. **DNS 重新綁定（DNS Rebinding）導致 SSRF 防護存在時間差（TOCTOU）繞過風險**：端點驗證與實際連線分離，攻擊者可藉由動態 DNS 刺探內部網段或本機服務。
3. **CSRF 與來源驗證邊界不完整**：缺少標準 Anti-CSRF Token，且所有 GET 端點均缺乏來源保護，敏感簡報與產出檔案可被跨站載入或外洩。
4. **多模態間接提示注入（Indirect Prompt Injection）**：簡報圖片內容直接輸入視覺語言模型（VLM），惡意簡報可能劫持解說稿生成。
5. **供應鏈與未釘選依賴風險**：AI 模組直接從 Git 儲存庫安裝未指定 Commit Hash 的 ChatTTS。

**整體風險評級 (Overall Risk Level)**：**🟡 MEDIUM-HIGH (中高風險)**  
*（若僅限單人本機封閉測試為中風險；若開放區域網路或未授權外部連線則為高風險）*

---

## 2. 弱點與風險發現總表 (Findings Table)

| 編號 | 嚴重程度 (Severity) | 檔案與行號 (File:Line) | 弱點類別 (Category) | 弱點描述 (Description) | 修補建議 (Recommendation) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **SEC-01** | 🔴 **HIGH** | `app/main.py:120-125, 215-280, 348-396` | 缺乏驗證與授權 (CWE-306 / Broken Access Control) | 全站所有 API（工作清單、簡報上傳、講稿編輯、音訊合成、工作刪除、模型金鑰設定）均未實作任何使用者身分驗證或存取權限控管。若伺服器非綁定於 `127.0.0.1`（例如綁定 `0.0.0.0` 或於容器內運行），網段內任何人皆可任意操作或讀取資料。 | 實作 API Token、Session 驗證或 HTTP Basic Auth；若僅供本機使用，應在程式碼層次強制拒絕非 loopback 連線，並確認 uvicorn 僅監聽 `127.0.0.1`。 |
| **SEC-02** | 🔴 **HIGH** | `app/settings.py:95-106, 280-345` | SSRF / DNS Rebinding (CWE-918 / CWE-367) | `validate_base_url` 在 Pydantic 驗證階段使用 `socket.getaddrinfo` 解析主機並拒絕私有 IP。然而在實際呼叫 `httpx.post` 時，`httpx` 會再次解析 DNS。攻擊者可利用極短 TTL 的 DNS Rebinding 服務，讓驗證時為公網 IP，實際連線時指向 `127.0.0.1` 或雲端 Metadata (`169.254.169.254`)。 | 在連線層實作 IP 釘選（IP Pinning），或透過自訂 `httpx.HTTPTransport` 攔截底層 socket 連線，確保連線目標 IP 與驗證通過之 IP 完全一致且非私有 IP。 |
| **SEC-03** | 🟡 **MEDIUM** | `app/main.py:87-117` | CSRF 防禦缺口與來源驗證不足 (CWE-352 / Weak Origin Check) | 1. `protect_local_mutations` 僅檢查變更請求，未實作標準 Anti-CSRF Token。<br>2. 當請求遺漏 `Origin` 與 `Referer` 時，會因進入 `elif request.headers.get("user-agent"): pass` 而放行。<br>3. 所有的 `GET` 端點（含讀取所有工作、投影片圖片、講稿、下載完整影片）完全不受來源檢查保護。 | 導入標準 CSRF Token 或強制要求特定自訂 Header（如 `X-Requested-With: XMLHttpRequest`）；在無 Origin/Referer 時預設拒絕變更請求；敏感 GET 操作應加強保護。 |
| **SEC-04** | 🟡 **MEDIUM** | `app/pipeline.py:167-182` | 間接提示注入 (CWE-1427 / OWASP LLM01: Prompt Injection) | 簡報轉出的投影片影像直接傳入視覺語言模型（VLM）。若投影片內嵌入對抗性指令（如覆寫指示的隱藏文字、提示劫持語句），模型可能順從指令輸出惡意釣魚連結或不良內容，並被後續流程合成語音與影片。 | 強化系統提示詞的指令與資料邊界；在介面強制落實人工審核（Human-in-the-loop）；對 LLM 輸出實施惡意連結與關鍵字安全過濾。 |
| **SEC-05** | 🟡 **MEDIUM** | `app/main.py:191-213, 220-253`, `app/jobs.py:82` | 無速率限制與資源耗盡風險 (CWE-400 / Denial of Service) | 全站缺乏速率限制（Rate Limiting）。背景工作佇列（ThreadPoolExecutor）未設定容量上限；惡意或頻繁的檔案上傳與語音測試呼叫，會耗盡 CPU/GPU（ChatTTS 推論相當沉重）與磁碟儲存空間。 | 導入 `slowapi` 等速率限制套件；限制背景佇列最大排隊長度；對音訊測試與上傳端點加上頻率限制。 |
| **SEC-06** | 🟡 **MEDIUM** | `requirements-ai.txt:4`, `requirements.txt` | 供應鏈與依賴完整性風險 (CWE-1357 / Supply Chain Risk) | `requirements-ai.txt` 直接由 `git+https://github.com/2noise/ChatTTS.git` 安裝，未鎖定明確的 Commit Hash 與雜湊值。若上游 GitHub 儲存庫遭篡改或惡意更新，在部署安裝時可能引入惡意程式碼（ChatTTS 涉及 PyTorch 權重載入）。 | 釘選明確的 Git Commit Hash，並使用具備 SHA256 雜湊檢查的 lockfile（如 `uv.lock` 或 `pip-compile`）固定相依套件版本。 |
| **SEC-07** | 🔵 **LOW** | `app/main.py:44-59` | 缺失安全性 HTTP 標頭 (CWE-1021 / CWE-693) | FastAPI 未配置安全性標頭，包括 `Content-Security-Policy` (CSP)、`X-Frame-Options`、`X-Content-Type-Options: nosniff` 及 `Referrer-Policy`。 | 透過中介軟體全域加入安全標頭，設定 `X-Frame-Options: DENY` 防止點擊劫持，並配置適當的 CSP。 |
| **SEC-08** | 🔵 **LOW** | `app/settings.py:284-345`, `app/pipeline.py:269-288` | 機敏資料外傳雲端隱私風險 (CWE-200 / CWE-359) | 當使用者選擇 Gemini、NVIDIA、OpenAI 或 Edge TTS 時，簡報內容（文字與影像）將上傳至第三方公有雲端服務，若簡報含有內部機密或個人隱私資料，有資料外洩風險。 | 在 Web 介面切換至非本機服務時，主動跳出顯著的隱私與資料外傳警示確認視窗。 |

---

## 3. 詳細弱點分析與修補指引 (Detailed Findings & Remediation Guide)

### 3.1 [SEC-01] 缺乏身分驗證與授權機制 (Broken Access Control)
- **問題分析**：
  應用程式原本設計為本機單人桌面應用程式，因此在 API 設計上假定「只要能存取 port 8000 均為可信任使用者」。在 `app/main.py` 中，任何連線至 `/api/jobs`、`/api/settings` 的用戶端，皆能建立工作、刪除既有工作、讀取其他工作的所有簡報圖片與產出音訊、修改 LLM 端點與 API Key。若使用者將其部署於公司內網、局域網伺服器或 Docker 容器且未做額外網路隔離，內網中的任何人皆具備完整系統操作與資料竊取權限。
- **修補建議**：
  1. 若維持本機桌面工具定位，必須在 `app/main.py` 啟動或中介軟體層強制檢查 `request.client.host`，僅允許 loopback 位址（`127.0.0.1`, `::1`）。
  2. 若有網路共用或多人使用需求，需引入輕量驗證機制（例如在啟動時自動產生隨機 Session Token 或支援 API Key / Bearer 認證），並在所有 `/api/*` 端點強制校驗。

---

### 3.2 [SEC-02] DNS 重新綁定 (DNS Rebinding) 導致 SSRF 防護繞過
- **問題分析**：
  `app/settings.py` 內的 `ProviderConfig.validate_base_url` 會執行：
  ```python
  addr_infos = socket.getaddrinfo(host, None)
  for _, _, _, _, sockaddr in addr_infos:
      ip_str = sockaddr[0]
      ip_obj = ipaddress.ip_address(ip_str)
      if not is_loopback_name and not ip_obj.is_global:
          raise ValueError("服務端點網域不可解析至私人或保留 IP 位址。")
  ```
  此檢查僅在使用者儲存設定時觸發一次。當系統在 `test_provider_connection` 或 `generate_script` 呼叫 `httpx.post(f"{config.base_url}/...")` 時，`httpx` 會重新透過系統 resolver 進行 DNS 解析。
  攻擊者可註冊一個具備極短 TTL（例如 0 秒）的網域名稱：
  - 第 1 次查詢（Pydantic 驗證）：回傳公網 IP（如 `93.184.216.34`），通過 `ip_obj.is_global` 檢查。
  - 第 2 次查詢（HTTP 連線發起）：回傳 `127.0.0.1`、`169.254.169.254` 或內部伺服器 IP。
  結果 `httpx` 將向內部受保護服務發出 HTTP 請求，甚至在標頭中攜帶使用者的 API Key，造成 SSRF 或憑證外洩。
- **修補建議**：
  實作自訂的 `httpx.HTTPTransport`，覆寫底層連線建立邏輯，在建立 TCP/TLS socket 的當下檢驗實際連線之 IP 位址（確保其為公網 IP 且非保留 IP），從根本杜絕 DNS TOCTOU 漏洞。

---

### 3.3 [SEC-03] CSRF 防禦缺口與來源驗證不足
- **問題分析**：
  在 `app/main.py` 的 `protect_local_mutations` 中：
  ```python
  elif request.headers.get("user-agent"):
      # Browser mutation without Origin/Referer in local context
      pass
  ```
  當瀏覽器發出的請求因特定情境（如 `<meta name="referrer" content="no-referrer">` 或某些不傳送 Origin 的情境）缺少 `Origin` 與 `Referer` 時，中介軟體會跳過拒絕邏輯。此外，本系統未採用 Anti-CSRF Token，且所有的 `GET` 端點完全不檢查來源，攻擊者可在惡意網頁中藉由跨站請求直接獲取使用者已完成的簡報內容。
- **修補建議**：
  1. 對於所有寫入/變更方法（POST/PUT/DELETE），強制檢查必須具備自訂標頭（例如 `X-Requested-With: XMLHttpRequest` 或 `X-Studio-Client: 1`），瀏覽器跨站請求無法在未經 CORS 允許下隨意附加非簡單標頭。
  2. 若無 `Origin` 且無 `Referer`，在嚴格模式下應拒絕請求或要求驗證 Token。

---

### 3.4 [SEC-04] 多模態間接提示注入 (Indirect Prompt Injection)
- **問題分析**：
  `app/pipeline.py` 將使用者上傳之簡報渲染為圖片，連同固定提示詞傳送至 VLM 模型（Qwen-VL、Gemini、GPT-4o-mini 等）。若使用者處理來自不明來源的簡報（例如網路下載的演講投影片），投影片中若包含精心設計的對抗性文字（Visual Prompt Injection），可能誘使 VLM 忽略原本的指示，產生惡意台詞、虛假資訊或釣魚宣傳。
- **修補建議**：
  1. 系統現有的 `SCRIPT_REVIEW`（講稿審核階段）是極佳的縱深防禦措施，應在 UI 介面上以醒目提示提醒使用者務必審核 AI 生成之台詞。
  2. 增加輸出內容後處理過濾器，自動偵測並過濾台詞中的 URL 連結、敏感指令字詞。

---

### 3.5 [SEC-05] 缺乏速率限制與資源耗盡風險 (DoS)
- **問題分析**：
  1. `app/jobs.py` 中 `self.executor = ThreadPoolExecutor(max_workers=1)` 雖然防止了並行 CPU 爭搶，但其預設的任務佇列（Queue）為無界佇列（Unbounded Queue）。連續提交 100 個工作將全數進入排程，長時間佔用伺服器。
  2. `/api/settings/test-tts` 每次被觸發都會在工作目錄產生暫存音訊並執行 TTS 轉換，若遭連續觸發將造成高 CPU/GPU 負載。
- **修補建議**：
  1. 引入 `slowapi` 為關鍵端點配置速率限制（例如上傳簡報每分鐘最多 5 次、測試連線每分鐘最多 10 次）。
  2. 為工作執行緒池設定有界佇列，當排隊任務超過特定上限時回傳 `HTTP 429 Too Many Requests`。

---

### 3.6 [SEC-06] 供應鏈與依賴完整性風險
- **問題分析**：
  `requirements-ai.txt` 宣告：
  `git+https://github.com/2noise/ChatTTS.git`
  此方式會每次抓取該儲存庫 `default` 分支之最新狀態。一旦該儲存庫被攻破、維護者帳號遭盜用，或發布了不相容更新，安裝時將直接下載受污染之 Python 程式碼。由於 PyTorch 模型權重普遍採用 Pickle 格式（`torch.load`），惡意權重可能在載入階段執行任意程式碼。
- **修補建議**：
  1. 釘選具體的 Git Commit Hash，例如：  
     `git+https://github.com/2noise/ChatTTS.git@<commit_hash>`
  2. 優先建議使用官方發布於 PyPI 之版本，或使用 `uv pip compile --generate-hashes` 建立具備 SHA256 校驗的鎖定檔。

---

## 4. 總結與防禦改善優先序 (Conclusion & Action Plan)

| 優先序 | 改善項目 | 預期防護效益 | 建議時程 |
| :---: | :--- | :--- | :---: |
| **P1** | **限制網路監聽介面與存取控制**：確保預設僅綁定 `127.0.0.1`，並加入基礎身分認證或本機嚴格連線過濾。 | 防止區域網路未授權人員存取機敏簡報與模型設定。 | 立即執行 |
| **P2** | **強化 API 來源防護與 CSRF Token**：加入自訂 Header 驗證與嚴格來源校驗。 | 消除跨站請求偽造與本機 API 被惡意網頁操控之風險。 | 本週內 |
| **P3** | **SSRF 連線階段 IP 檢驗 (IP Pinning)**：自訂 HTTP 傳輸層以阻擋 DNS Rebinding。 | 防範利用自訂模型端點刺探內部網段或元數據服務。 | 次週內 |
| **P4** | **鎖定相依套件版本與 Commit Hash**：更新 `requirements*.txt` 鎖定 ChatTTS 提交版本。 | 降低開源供應鏈受污染與惡意程式碼注入風險。 | 次週內 |
| **P5** | **配置安全性回應標頭與速率限制**：加入 CSP、X-Frame-Options 及 `slowapi` 限速。 | 提升整體應用程式縱深防禦能力與抵禦 DoS 攻擊。 | 下次版本發布 |

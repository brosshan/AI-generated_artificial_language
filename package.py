#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 簡報導讀影音工作台 - 自動化純淨打包工具 (Safe Distribution Packager)
----------------------------------------------------------------------
功能：
1. 嚴格白名單與黑名單過濾：自動排除 workspace/ (使用者簡報與影音)、
   asset/ (大模型權重)、.venv/、__pycache__、.env 機密檔案與暫存檔案。
2. 資安前置門禁掃描 (Pre-flight Security Audit)：
   打包前全面檢測所有將收錄的檔案，嚴禁任何 API Key、私鑰或真實密碼外洩。
3. 自動產出跨平台無污染的發布壓縮包 (ZIP / TAR.GZ)，便於移轉至其他伺服器部署。
"""

import argparse
import datetime
import os
import re
import sys
import tarfile
import zipfile
from pathlib import Path

# 確保在 Windows CP950 終端機環境下安全輸出 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 專案版本號
VERSION = "1.0.0"

# 根目錄與發布目錄
ROOT_DIR = Path(__file__).resolve().parent
DIST_DIR = ROOT_DIR / "dist"

# 明確收錄的根目錄檔案白名單
ROOT_ALLOWLIST = {
    "README.md",
    "SKILL.md",
    "agent.md",
    "requirements.txt",
    "requirements-ai.txt",
    "start.bat",
    "start.sh",
    ".env.example",
    ".gitignore",
    "package.py",
    "package.bat",
    "SECURITY-REVIEW.md",
    "SECURITY-ANALYSIS-REPORT.md",
    "SECURITY-REMEDIATION-VERIFICATION.md",
}

# 明確收錄的子目錄
DIR_ALLOWLIST = {"app", "tests"}

# 絕對禁止收錄的目錄 (Blacklist Directories)
FORBIDDEN_DIRS = {
    "workspace",
    "asset",
    ".venv",
    "venv",
    "env",
    "ENV",
    ".pytest_cache",
    ".herdr-reports",
    "__pycache__",
    ".git",
    "dist",
    "build",
    "libreoffice-profile",
}

# 絕對禁止收錄的檔案副檔名或檔案名稱 (Blacklist Files)
FORBIDDEN_EXTENSIONS = {
    ".pyc", ".pyo", ".pyd",
    ".safetensors", ".bin", ".pt", ".pth", ".onnx",
    ".pdf", ".pptx", ".ppt",
    ".mp4", ".mp3", ".wav",
    ".bak", ".tmp", ".log", ".swp",
    ".key", ".pem", ".p12", ".cert", ".crt",
}

# 資安 Regex 檢測規則
SECRET_PATTERNS = [
    ("Google API Key", re.compile(r"AIza[0-9A-Za-z-_]{35}")),
    ("OpenAI API Key", re.compile(r"sk-[a-zA-Z0-9]{20,}")),
    ("NVIDIA NIM Key", re.compile(r"nvapi-[a-zA-Z0-9_-]{20,}")),
    ("Anthropic API Key", re.compile(r"sk-ant-[a-zA-Z0-9_-]{20,}")),
    ("GitHub Token", re.compile(r"(ghp_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9_]{50,})")),
    ("Private Key Block", re.compile(r"-----BEGIN (RSA|EC|OPENSSH|DSA|PGP|ENCRYPTED|PRIVATE) KEY-----")),
    ("Hardcoded Password", re.compile(r"(?i)(password|passwd|pwd)\s*[:=]\s*[\"'][^\"']+[\"']")),
]

# 安全的單元測試假值白名單 (忽略安全掃描警報)
SAFE_TEST_MOCKS = {
    "mock-test-key-12345",
    "secret-for-test",
    "test-key",
    "dummy",
    "example",
    "placeholder",
}


def is_file_allowed(rel_path: Path) -> bool:
    """判斷檔案是否允許收錄進發布包。"""
    parts = rel_path.parts
    # 檢查是否位於被禁用的資料夾內
    for part in parts:
        if part in FORBIDDEN_DIRS or (part.startswith(".env") and part != ".env.example"):
            return False

    # 檢查副檔名
    if rel_path.suffix.lower() in FORBIDDEN_EXTENSIONS:
        return False

    # 檢查根目錄檔案
    if len(parts) == 1:
        return parts[0] in ROOT_ALLOWLIST

    # 檢查允許的頂層子目錄
    if parts[0] in DIR_ALLOWLIST:
        return True

    return False


def collect_distribution_files() -> list[Path]:
    """收集所有符合白名單且排除機密的專案檔案。"""
    collected = []
    for root, dirs, files in os.walk(ROOT_DIR):
        # 原地過濾目錄，避免進入黑名單目錄節省遍歷時間
        dirs[:] = [d for d in dirs if d not in FORBIDDEN_DIRS and not d.startswith(".env")]
        
        rel_root = Path(root).relative_to(ROOT_DIR)
        for f in files:
            rel_file = rel_root / f if str(rel_root) != "." else Path(f)
            if is_file_allowed(rel_file):
                collected.append(rel_file)
                
    collected.sort()
    return collected


def run_security_scan(files: list[Path]) -> bool:
    """在打包前全面執行資安與金鑰門禁掃描。"""
    print("\n[SCAN] 正在進行打包前資安門禁掃描 (Pre-flight Security Audit)...")
    violations = []

    for rel_path in files:
        full_path = ROOT_DIR / rel_path
        # 僅掃描文字類檔案
        if full_path.suffix.lower() in {".py", ".html", ".js", ".css", ".md", ".txt", ".json", ".bat", ".sh", ".example"}:
            try:
                content = full_path.read_text(encoding="utf-8", errors="ignore")
            except Exception as e:
                print(f"  [WARN] 無法讀取檔案 {rel_path}: {e}")
                continue

            for line_no, line in enumerate(content.splitlines(), start=1):
                # 排除純註解或範例
                for rule_name, pattern in SECRET_PATTERNS:
                    matches = pattern.findall(line)
                    for match in matches:
                        match_str = match if isinstance(match, str) else str(match)
                        # 檢查是否為安全單元測試 Mock
                        if any(mock in line.lower() for mock in SAFE_TEST_MOCKS):
                            continue
                        violations.append((rel_path, line_no, rule_name, match_str[:12] + "..."))

    if violations:
        print("\n[ERROR] 在即將打包的檔案中偵測到潛在機密資料！打包程序已強制終止！")
        for f, l, r, m in violations:
            print(f"  - [{r}] 於 {f} (第 {l} 行): 疑似機密字串 [{m}]")
        print("\n請先清除上述機密資料後再重新執行打包。")
        return False

    print("  [OK] 資安門禁審查通過：未發現任何真實 API Key、密碼或憑證金鑰。")
    return True


def create_zip_archive(files: list[Path], output_path: Path):
    """建立乾淨的 ZIP 發布壓縮檔。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zipf:
        for rel_path in files:
            full_path = ROOT_DIR / rel_path
            archive_name = Path("ai-presentation-studio") / rel_path
            zipf.write(full_path, arcname=str(archive_name))
            

def create_tar_archive(files: list[Path], output_path: Path):
    """建立適用於 Linux / Docker 部署的 TAR.GZ 壓縮檔。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output_path, "w:gz") as tarf:
        for rel_path in files:
            full_path = ROOT_DIR / rel_path
            archive_name = Path("ai-presentation-studio") / rel_path
            tarf.add(full_path, arcname=str(archive_name))


def verify_archive(archive_path: Path):
    """驗證產出的發布包內容。"""
    print(f"\n[VERIFY] 正在驗證發布檔：{archive_path.name}")
    size_mb = archive_path.stat().st_size / (1024 * 1024)
    
    with zipfile.ZipFile(archive_path, "r") as zipf:
        namelist = zipf.namelist()
        total_files = len(namelist)
        
        # 嚴格驗證黑名單檔案是否誤入
        forbidden_found = [
            name for name in namelist 
            if any(bad in name.lower() for bad in ["workspace/", "asset/", ".venv/", ".safetensors"])
        ]
        if forbidden_found:
            raise RuntimeError(f"驗證失敗！發布包內包含被禁止的項目: {forbidden_found[:3]}")
            
    print(f"  [OK] 驗證成功！共收錄 {total_files} 個純淨檔案，壓縮大小為 {size_mb:.2f} MB。")


def main():
    parser = argparse.ArgumentParser(description="AI 簡報導讀影音工作台 - 純淨發布打包工具")
    parser.add_argument("--format", choices=["zip", "tar", "all"], default="zip", help="輸出格式 (預設: zip)")
    parser.add_argument("--skip-scan", action="store_true", help="跳過資安掃描 (不建議)")
    args = parser.parse_args()

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    print("=" * 66)
    print("  AI 簡報導讀影音工作台 - 自動打包與機密排查工具")
    print(f"  版本：v{VERSION}  |  時間：{timestamp}")
    print("=" * 66)

    # 1. 收集檔案
    files = collect_distribution_files()
    print(f"[COLLECT] 共篩選出 {len(files)} 個允許收錄的純淨原始檔。")

    # 2. 資安前置檢測
    if not args.skip_scan:
        if not run_security_scan(files):
            sys.exit(1)

    # 3. 建立封裝檔
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    
    outputs = []
    if args.format in {"zip", "all"}:
        zip_path = DIST_DIR / f"ai-presentation-studio-v{VERSION}-{timestamp}.zip"
        latest_zip = DIST_DIR / "ai-presentation-studio-release.zip"
        print(f"\n[BUILD] 正在建立 ZIP 壓縮包: {zip_path.name} ...")
        create_zip_archive(files, zip_path)
        create_zip_archive(files, latest_zip)
        verify_archive(zip_path)
        outputs.append(zip_path)
        outputs.append(latest_zip)

    if args.format in {"tar", "all"}:
        tar_path = DIST_DIR / f"ai-presentation-studio-v{VERSION}-{timestamp}.tar.gz"
        print(f"\n[BUILD] 正在建立 Linux TAR.GZ 壓縮包: {tar_path.name} ...")
        create_tar_archive(files, tar_path)
        outputs.append(tar_path)

    print("\n" + "=" * 66)
    print("打包作業全數順利完成！")
    print(f"輸出目標目錄: {DIST_DIR}")
    for out in outputs:
        print(f"  -> {out.name} ({out.stat().st_size / (1024 * 1024):.2f} MB)")
    print("\n移轉至其他機器或伺服器的部署方式：")
    print("  1. 將產生的 ZIP 解壓縮至目標主機。")
    print("  2. Windows 主機：雙擊 start.bat 即可一鍵建立環境並啟動服務。")
    print("  3. Linux / macOS 主機：執行 ./start.sh 即可自動部署與運行。")
    print("=" * 66)


if __name__ == "__main__":
    main()

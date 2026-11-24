"""断点续传下载 GitHub CLI（winget 不走本机代理，这里用 Python 走代理）。

    python scripts/fetch_gh.py

下载到 %TEMP%/gh.zip 并解压出 gh.exe。
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

VERSION = "2.103.3"
URL = (
    f"https://github.com/cli/cli/releases/download/v{VERSION}/"
    f"gh_{VERSION}_windows_amd64.zip"
)
PROXY = os.environ.get("AERODATA_HTTPS_PROXY", "http://127.0.0.1:9674")


def download(url: str, target: Path, attempts: int = 40) -> bool:
    """带 Range 续传的下载循环 —— 代理链路不稳定，需要能接着上次断点拉。"""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"https": PROXY, "http": PROXY})
    )
    for attempt in range(attempts):
        have = target.stat().st_size if target.exists() else 0
        headers = {"User-Agent": "aerodata-setup"}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=60) as resp:
                mode = "ab" if (have and resp.status == 206) else "wb"
                if mode == "wb":
                    have = 0
                with target.open(mode) as handle:
                    while True:
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        handle.write(chunk)
            total = target.stat().st_size
            print(f"[download] 完成，共 {total / 1024 / 1024:.1f} MB")
            return True
        except Exception as exc:  # noqa: BLE001
            now = target.stat().st_size if target.exists() else 0
            print(f"[download] 第 {attempt + 1} 次中断: {type(exc).__name__}（已下载 {now / 1024 / 1024:.1f} MB）")
            time.sleep(2)
    return False


def main() -> int:
    tmp = Path(os.environ.get("TEMP", ".")) / "gh_dl"
    tmp.mkdir(parents=True, exist_ok=True)
    archive = tmp / "gh.zip"

    if not archive.exists() or archive.stat().st_size < 1_000_000:
        print(f"[download] {URL}")
        if not download(URL, archive):
            print("[download] 失败")
            return 1

    extract_dir = tmp / "gh"
    if extract_dir.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(extract_dir)

    for exe in extract_dir.rglob("gh.exe"):
        dest = Path(os.environ.get("LOCALAPPDATA", tmp)) / "Programs" / "GitHub CLI" / "gh.exe"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(exe, dest)
        print(f"[done] gh.exe -> {dest}")
        return 0
    print("[done] 压缩包里没找到 gh.exe")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

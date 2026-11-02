"""Cookie handling: browser extraction / cookies.txt / Netscape file for yt-dlp.

- 浏览器 cookie 解密按“设置哈希”缓存：同一配置下每个视频只解密一次，
  避免 fetch_video_info + fetch_subtitles + write_cookiefile 三次重复解密。
- Netscape cookie 文件不再散落 %TEMP%：复用 data/cache 下单一文件，
  退出时（atexit）删除，不让明文 SESSDATA 在磁盘上过夜。
"""
from __future__ import annotations

import atexit
import hashlib
import threading
from pathlib import Path
from typing import Optional

from .config import Settings, cache_dir


def _from_browser(browser: str) -> dict[str, str]:
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except Exception:
        return {}
    try:
        jar = extract_cookies_from_browser(browser, keyring=None, logger=None)
    except Exception:
        return {}
    out = {}
    for c in jar:
        if c.name and "bilibili" in (c.domain or "").lower():
            out[c.name] = c.value
    return out


def _from_file(path: Path) -> dict[str, str]:
    out = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            name, value = parts[5], parts[6]
            if name:
                out[name] = value
    return out


def _from_string(raw: str) -> dict[str, str]:
    """从 DevTools 复制的 Cookie 头字符串解析，如 `SESSDATA=xxx; bili_jct=yyy`。"""
    out = {}
    for chunk in raw.replace("\n", ";").replace("\r", ";").split(";"):
        chunk = chunk.strip()
        if "=" in chunk:
            k, _, v = chunk.partition("=")
            k = k.strip()
            v = v.strip().strip("\"")
            if k:
                out[k] = v
    return out


def _settings_key(settings: Settings) -> str:
    raw = "|".join([settings.cookies_source, settings.cookies_browser,
                    settings.cookies_file, settings.cookies_string])
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


_cache_lock = threading.Lock()
_cache_key: Optional[str] = None
_cache_cookies: dict[str, str] = {}


def get_cookies(settings: Settings) -> dict[str, str]:
    global _cache_key, _cache_cookies
    if settings.cookies_source == "browser":
        # 浏览器 DB 解密又慢又容易锁：同配置只解一次
        key = _settings_key(settings)
        with _cache_lock:
            if _cache_key == key:
                return dict(_cache_cookies)
        fresh = _from_browser(settings.cookies_browser)
        with _cache_lock:
            _cache_key, _cache_cookies = key, dict(fresh)
        return fresh
    if settings.cookies_source == "file" and settings.cookies_file:
        return _from_file(Path(settings.cookies_file))
    if settings.cookies_source == "string" and settings.cookies_string.strip():
        return _from_string(settings.cookies_string)
    return {}


def cookie_header(settings: Settings) -> str:
    c = get_cookies(settings)
    return "; ".join(f"{k}={v}" for k, v in c.items())


def _managed_cookiefile() -> Path:
    p = cache_dir() / "yt_cookies.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _cleanup_cookiefile() -> None:
    try:
        _managed_cookiefile().unlink(missing_ok=True)
    except Exception:
        pass


atexit.register(_cleanup_cookiefile)


def write_cookiefile(settings: Settings) -> Optional[Path]:
    """Return path to a Netscape cookiefile for yt-dlp, or None.

    单一复用文件（data/cache/yt_cookies.txt），退出时删除；
    不再每次调用都在 %TEMP% 留一个明文 cookie 文件。
    """
    if settings.cookies_source == "file" and settings.cookies_file:
        p = Path(settings.cookies_file)
        if p.exists():
            return p
    c = get_cookies(settings)
    if not c:
        _cleanup_cookiefile()
        return None
    target = _managed_cookiefile()
    body = ["# Netscape HTTP Cookie File"]
    body += [f".bilibili.com\tTRUE\t/\tFALSE\t0\t{k}\t{v}" for k, v in c.items()]
    target.write_text("\n".join(body) + "\n", encoding="utf-8")
    return target

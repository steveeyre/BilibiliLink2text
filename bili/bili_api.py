"""Bilibili API client: subtitle chain, watch-later / favorites scanning, URL parsing."""
from __future__ import annotations

import re
import requests
from typing import Optional

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
REFERER = "https://www.bilibili.com"
TIMEOUT = 15

BV_RE = re.compile(r"BV[0-9A-Za-z]+")
P_RE = re.compile(r"[?&]p=(\d+)")


class BiliError(Exception):
    pass


def _session(cookie_header: str = "") -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": REFERER})
    if cookie_header:
        s.headers["Cookie"] = cookie_header
    return s


def _get_json(s: requests.Session, url: str, params: dict) -> dict:
    r = s.get(url, params=params, timeout=TIMEOUT)
    try:
        data = r.json()
    except Exception:
        raise BiliError(f"接口返回异常: {r.status_code} {url}")
    if data.get("code") not in (0, None):
        msg = data.get("message") or data.get("msg") or "未知错误"
        raise BiliError(f"B站接口错误 {data.get('code')}: {msg}")
    return data


def _resolve_short(url: str) -> str:
    if re.search(r"(b23\.tv|bili22\.cn|bili23\.cn)", url, re.I):
        try:
            r = requests.head(url, allow_redirects=True, timeout=TIMEOUT, headers={"User-Agent": UA})
            return r.url
        except Exception:
            pass
    return url


def parse_video_url(url: str) -> tuple[str, int]:
    """Return (bvid, p). Accepts short links, watch-later / favorites list URLs."""
    url = _resolve_short(url.strip())
    m = BV_RE.search(url)
    if not m:
        raise BiliError(f"无法从链接中解析出 BV 号: {url}")
    bvid = m.group(0)
    p = 1
    pm = P_RE.search(url)
    if pm:
        p = int(pm.group(1))
    else:
        tm = re.search(r"[/?&]p(\d+)(?:[/?&]|$)", url)
        if tm:
            p = int(tm.group(1))
    return bvid, p


def fetch_video_info(bvid: str, p: int = 1, cookie_header: str = "") -> dict:
    s = _session(cookie_header)
    data = _get_json(s, "https://api.bilibili.com/x/web-interface/view", {"bvid": bvid})
    d = data["data"]
    pages = d.get("pages") or []
    page = pages[p - 1] if pages else None
    return {
        "bvid": bvid,
        "title": d.get("title", ""),
        "desc": d.get("desc", ""),
        "owner": d.get("owner", {}).get("name", ""),
        "duration": int(d.get("duration") or 0),
        "pubdate": d.get("pubdate", 0),
        "pic": d.get("pic", ""),
        "cid": page["cid"] if page else d.get("cid", 0),
        "part": (page or {}).get("part", ""),
        "page_count": len(pages) or 1,
    }


def fetch_subtitles(bvid: str, cid: int, cookie_header: str = "") -> Optional[list[dict]]:
    """Return [{start, end, text}] or None. 人工字幕 > AI 字幕 > 任意."""
    s = _session(cookie_header)
    data = _get_json(s, "https://api.bilibili.com/x/player/wbi/v2", {"bvid": bvid, "cid": cid})
    subs = (data.get("data") or {}).get("subtitle") or {}
    subtitles = subs.get("subtitles") or []
    if not subtitles:
        return None

    def is_zh(lan: str) -> bool:
        return bool(lan and lan.lower().startswith("zh"))

    chosen = None
    for st in subtitles:
        if is_zh(st.get("lan", "")) and not st.get("ai_type"):
            chosen = st
            break
    if chosen is None:
        for st in subtitles:
            if is_zh(st.get("lan", "")):
                chosen = st
                break
    if chosen is None:
        chosen = subtitles[0]

    u = chosen.get("subtitle_url") or ""
    if not u:
        if chosen.get("ai_type") and not cookie_header:
            raise BiliError("该视频的 AI 字幕需要登录（配置 cookies 后再试）")
        return None
    if u.startswith("//"):
        u = "https:" + u
    body = _get_json(s, u, {})
    segs = body.get("body") or []
    out = []
    for it in segs:
        text = (it.get("content") or "").strip()
        if text:
            out.append({"start": float(it.get("from", 0)), "end": float(it.get("to", 0)), "text": text})
    return out or None


def fetch_watch_later(cookie_header: str, limit: int = 500) -> list[dict]:
    """拉取「稍后再看」全部条目。

    接口在支持 pn/ps 时会分页；不支持时会忽略参数返回同一批数据，
    此时靠 `page 为空` 终止，不会死循环也不会漏数据。
    """
    if not cookie_header:
        raise BiliError("「稍后再看」需要登录 cookies")
    s = _session(cookie_header)
    out, seen = [], set()
    pn, ps = 1, 20
    while len(out) < limit:
        data = _get_json(s, "https://api.bilibili.com/x/v2/history/toview",
                         {"pn": pn, "ps": ps})
        d = data.get("data") or {}
        items = d.get("list") or []
        page = []
        for it in items:
            bvid = it.get("bvid", "")
            if not bvid or bvid in seen:
                continue
            seen.add(bvid)
            page.append({"bvid": bvid, "title": it.get("title", ""),
                         "duration": int(it.get("duration") or 0)})
        out.extend(page)
        total = int(d.get("count") or 0)
        if not page or len(items) < ps or (total and len(out) >= total):
            break
        pn += 1
    return out[:limit]


def _current_mid(s: requests.Session) -> int:
    data = _get_json(s, "https://api.bilibili.com/x/web-interface/nav", {})
    d = data.get("data") or {}
    if not d.get("isLogin"):
        raise BiliError("cookies 未登录，请重新获取有效 cookies")
    return int(d.get("mid") or 0)


def fetch_fav_folders(cookie_header: str) -> list[dict]:
    if not cookie_header:
        raise BiliError("「收藏夹」需要登录 cookies")
    s = _session(cookie_header)
    mid = _current_mid(s)
    try:
        data = _get_json(s, "https://api.bilibili.com/x/v3/fav/folder/created/list-all",
                         {"up_mid": mid})
    except BiliError as e:
        raise BiliError(f"{e}。收藏夹接口需要完整有效的登录 cookies（含 SESSDATA），"
                        f"请到 设置→Cookies 重新获取") from e
    out = []
    for it in (data.get("data") or {}).get("list") or []:
        out.append({"media_id": it.get("id", 0), "title": it.get("title", ""),
                    "media_count": it.get("media_count", 0)})
    return out


def fetch_fav_list(media_id: int, cookie_header: str, limit: int = 2000,
                   on_progress=None) -> list[dict]:
    if not cookie_header:
        raise BiliError("「收藏夹」需要登录 cookies")
    s = _session(cookie_header)
    out, pn = [], 1
    while len(out) < limit:
        data = _get_json(s, "https://api.bilibili.com/x/v3/fav/resource/list",
                         {"media_id": media_id, "pn": pn, "ps": 20, "platform": "web"})
        medias = (data.get("data") or {}).get("medias") or []
        if not medias:
            break
        for it in medias:
            out.append({"bvid": it.get("bvid", ""), "title": it.get("title", ""),
                        "duration": int(it.get("duration") or 0)})
        if on_progress is not None:
            on_progress(len(out))
        if len(medias) < 20:
            break
        pn += 1
    return out

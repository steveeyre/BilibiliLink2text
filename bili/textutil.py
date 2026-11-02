"""Text utilities: paragraph merging, timestamps, MarkDown building."""
from __future__ import annotations

import bisect
import re
from typing import Optional

MERGE_GAP = 3.0          # 小于该间隔的相邻字幕合并为一段
HEADING_RE = re.compile(r"^(第[\d一二三四五六七八九十百]+[章节回部分篇]|Part\s*\d+|Chapter\s*\d+|目录|章节)", re.I)
# 锚点匹配时忽略中英文标点：模型复述锚点常自行“修正”标点
PUNCT_RE = re.compile(r"[\s，。、；：！？,.;:!?\"'“”‘’（）()《》〈〉【】\[\]…—－\-~～]")


def fmt_ts(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def merge_segments(segments: list[dict]) -> list[dict]:
    """把时间相邻的字幕合并为自然段。返回 [{start, end, text}]。"""
    out: list[dict] = []
    for seg in segments:
        if out and seg["start"] - out[-1]["end"] < MERGE_GAP:
            out[-1]["end"] = seg["end"]
            out[-1]["text"] += seg["text"]
        else:
            out.append({"start": seg["start"], "end": seg["end"], "text": seg["text"]})
    return out


def split_chunks(text: str, limit: int = 1800) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in paras:
        if cur and len(cur) + len(p) + 1 > limit:
            chunks.append(cur)
            cur = p
        else:
            cur = (cur + "\n\n" + p) if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def is_heading_line(line: str) -> bool:
    return bool(HEADING_RE.match(line.strip()))


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", "", s)


def ensure_chapters_cover(proofread: str, chapters: Optional[list[dict]],
                          min_ratio: float = 0.85) -> Optional[list[dict]]:
    """章节覆盖率兜底：保证使用 chapters 时不静默丢失正文尾部。

    chapterize 正常走锚点切分（由 slice_by_anchors 保证 100% 覆盖）；但当模型
    不守格式、退回“标题 + 正文回显”解析时，拼接文本仍可能短于校对文稿。
    调用方（复制 Markdown / 导出 .md）优先使用 chapters，缺尾部会直接丢字
    且无任何提示。本函数在覆盖率不足时，定位最后章节锚点并把未覆盖的尾部
    补成一个「未分段」章节；若实在定位不到尾部，则返回 None（调用方回退到
    全文），宁可丢章节标题也不丢正文。
    """
    if not chapters:
        return None
    if not proofread or not proofread.strip():
        return chapters
    norm_proof = _norm_text(proofread)
    if not norm_proof:
        return chapters
    norm_concat = _norm_text("".join(c.get("text", "") for c in chapters))
    if len(norm_concat) >= min_ratio * len(norm_proof):
        return chapters
    # 定位尾部：末章末尾锚点在原文中出现的位置里，取最接近期望覆盖点的一处
    # （rfind 在重复文本下会误命中全文末尾；按期望位置就近匹配才准）
    norm_all = _norm_text("".join(c.get("text", "") for c in chapters))
    last_text = (chapters[-1].get("text") or "").strip()
    norm_last = _norm_text(last_text)
    tail = ""
    for anchor_len in (60, 40, 20):
        anchor = norm_last[-anchor_len:] if len(norm_last) > anchor_len else norm_last
        if not anchor:
            continue
        expected = max(0, len(norm_all) - len(anchor))
        best, best_dist = -1, None
        start = norm_proof.find(anchor)
        while start >= 0:
            dist = abs(start - expected)
            if best_dist is None or dist < best_dist:
                best, best_dist = start, dist
                if dist == 0:
                    break
            start = norm_proof.find(anchor, start + 1)
        if best < 0:
            continue
        covered = best + len(anchor)
        # norm 索引映射回原文索引：记录每个非空白字符在原文中的位置
        mapping = [i for i, ch in enumerate(proofread) if not ch.isspace()]
        if covered < len(mapping):
            tail = proofread[mapping[covered]:].strip()
        break
    if tail and len(_norm_text(tail)) > 50:
        chapters = list(chapters) + [{"title": "未分段（后续内容）", "text": tail}]
        return chapters
    return None


def _norm_with_map(s: str, drop_punct: bool = False) -> tuple[str, list[int]]:
    """返回归一化文本，以及每个保留字符在原文中的下标。"""
    chars: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(s):
        if ch.isspace():
            continue
        if drop_punct and PUNCT_RE.match(ch):
            continue
        chars.append(ch)
        idx.append(i)
    return "".join(chars), idx


def _find_anchor(norm: str, mapping: list[int], probe: str, cursor: int) -> int:
    """从 cursor（原文下标）起找锚点，返回原文下标；找不到返回 -1。"""
    if not probe:
        return -1
    start = bisect.bisect_left(mapping, cursor)
    # 模型复述时常加减标点或补尾巴，逐级缩短再试
    lengths = [len(probe), 40, 24, 16, 12, 8]
    for length in lengths:
        if length > len(probe):
            continue
        pos = norm.find(probe[:length], start)
        if pos >= 0:
            return mapping[pos]
    return -1


def slice_by_anchors(doc: str, anchors: list[tuple[str, str]],
                     min_chapters: int = 2) -> list[dict]:
    """按 (章节标题, 本章开头片段) 在原文上切分，结构上保证不丢正文。

    模型只给锚点、不回显全文，每章正文都是 doc 的连续切片：首章从 0 开始、
    末章到文末，拼接结果与 doc 一致。锚点定位不到时跳过该章（内容并入前章）；
    有效章节不足 min_chapters 时返回 []，由调用方回退全文。
    """
    if not doc or not anchors:
        return []
    strict, strict_map = _norm_with_map(doc)
    loose, loose_map = _norm_with_map(doc, drop_punct=True)
    if not strict:
        return []
    starts: list[tuple[str, int]] = []
    cursor = 0
    for title, anchor in anchors:
        title = (title or "").strip()
        probe = _norm_text(anchor or "")
        if not title or not probe:
            continue
        pos = _find_anchor(strict, strict_map, probe, cursor)
        if pos < 0:
            pos = _find_anchor(loose, loose_map, PUNCT_RE.sub("", probe), cursor)
        if pos < 0:
            continue
        starts.append((title, pos))
        cursor = pos + 1
    if len(starts) < min_chapters:
        return []
    chapters: list[dict] = []
    for i, (title, start) in enumerate(starts):
        begin = 0 if i == 0 else start
        end = starts[i + 1][1] if i + 1 < len(starts) else len(doc)
        text = doc[begin:end].strip()
        if text:
            chapters.append({"title": title, "text": text})
    return chapters if len(chapters) >= min_chapters else []


def build_markdown(title: str, proofread: str, chapters: Optional[list[dict]] = None) -> str:
    """界面「复制为 MarkDown」：干净文本，无时间戳，保留章节标题。"""
    parts = [f"# {title}", ""]
    chapters = ensure_chapters_cover(proofread, chapters)
    if chapters:
        for ch in chapters:
            parts.append(f"## {ch['title']}")
            parts.append("")
            parts.append(ch["text"].strip())
            parts.append("")
    else:
        parts.append(proofread.strip())
    return "\n".join(parts).strip() + "\n"


def build_export(info: dict, proofread: str, chapters: Optional[list[dict]],
                 segments: list[dict], summary: Optional[str], settings) -> str:
    """导出的 .md：可选原文稿 / 时间戳 / AI 总结。"""
    chapters = ensure_chapters_cover(proofread, chapters)
    parts: list[str] = []
    src = f"https://www.bilibili.com/video/{info['bvid']}"
    parts.append(f"# {info['title']}")
    parts.append("")
    parts.append(f"- 来源：{src}")
    parts.append(f"- UP主：{info.get('owner','')}")
    parts.append(f"- 时长：{fmt_ts(info.get('duration',0))}")
    parts.append("")

    if settings.export_summary and summary:
        parts.append("## AI 总结")
        parts.append("")
        parts.append(summary.strip())
        parts.append("")

    if settings.export_raw:
        parts.append("## 原文稿")
        parts.append("")
        if chapters:
            for ch in chapters:
                parts.append(f"### {ch['title']}")
                parts.append("")
                parts.append(ch["text"].strip())
                parts.append("")
        else:
            parts.append(proofread.strip())
            parts.append("")

    if settings.export_timestamps:
        parts.append("## 原片跳转时间戳")
        parts.append("")
        for seg in segments:
            snip = re.sub(r"\s+", "", seg["text"])[:60]
            parts.append(f"- [{fmt_ts(seg['start'])}] {snip}")
        parts.append("")

    return "\n".join(parts).strip() + "\n"

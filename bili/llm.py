"""LLM integration: OpenAI-compatible client (Ollama / API), 校对 / 章节 / 总结."""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from openai import OpenAI

from .config import Settings
from . import glossary
from . import llm_server
from .textutil import slice_by_anchors, split_chunks

CHUNK_LIMIT = 1800
CHAPTER_MIN_LEN = 2500
CHAPTER_INPUT_LIMIT = 12000   # 章节划分只需读开头足够内容即可，避免 CPU 上过慢
SUMMARY_INPUT_LIMIT = 32000
PROOFREAD_MAX_WORKERS = 3     # 分块校对并发上限（仅云端 API；本地模型与 ASR 抢 CPU，保持串行）


class LLMError(Exception):
    pass


class CancelledError(Exception):
    """用户主动取消（停止按钮 / 关闭窗口），调用方应视为取消而非失败。"""
    pass


def _check_cancel(cancel: Optional[threading.Event]) -> None:
    if cancel is not None and cancel.is_set():
        raise CancelledError("已取消")


def _client(settings: Settings) -> OpenAI:
    base = settings.llm_base_url().rstrip("/")
    key = settings.llm_api_key() or "ollama"
    return OpenAI(base_url=base, api_key=key, timeout=settings.llm_timeout)


def _chat_stream_client(client: OpenAI, model: str, system: str, user: str,
                        max_tokens: int = 4096,
                        on_delta: Optional[Callable[[str], None]] = None,
                        cancel: Optional[threading.Event] = None) -> str:
    """流式调用，逐 token 回调增量文本。慢速 CPU 推理也能看到实时进度。"""
    try:
        stream = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0.1,
            max_tokens=max_tokens,
            stream=True,
        )
        parts: list[str] = []
        for chunk in stream:
            _check_cancel(cancel)
            try:
                delta = chunk.choices[0].delta.content
            except Exception:
                delta = None
            if delta:
                parts.append(delta)
                if on_delta:
                    on_delta(delta)
        return "".join(parts).strip()
    except Exception as e:
        raise LLMError(f"大模型调用失败（{model}）: {str(e)[-200:]}") from e


def _chat_stream(settings: Settings, system: str, user: str, max_tokens: int = 4096,
                 on_delta: Optional[Callable[[str], None]] = None,
                 cancel: Optional[threading.Event] = None) -> str:
    """流式调用，逐 token 回调增量文本。慢速 CPU 推理也能看到实时进度。"""
    try:
        model = settings.llm_model()
        if settings.llm_provider == "llamacpp":
            llm_server.ensure_running(settings)
            model = llm_server.model_id(settings)
        client = _client(settings)
        return _chat_stream_client(client, model, system, user, max_tokens, on_delta, cancel)
    except LLMError:
        raise
    except Exception as e:
        hint = ""
        if settings.llm_provider == "ollama":
            hint = "。请确认 Ollama 已启动（右下角有小羊驼图标），并到 设置→大模型 核对模型名"
        elif settings.llm_provider == "llamacpp":
            hint = "。请到 设置→大模型 检查 llama-server 与 GGUF 路径，并查看日志"
        raise LLMError(f"大模型调用失败（{settings.llm_model()}）{hint}: {str(e)[-200:]}") from e


def _chat(settings: Settings, system: str, user: str, max_tokens: int = 4096) -> str:
    return _chat_stream(settings, system, user, max_tokens)


PROOF_SYSTEM = (
    "你是专业的文字校对员。用户会给你一段B站视频的中文语音转写文稿，"
    "可能存在同音错字、口语赘词、缺标点、断句错误等问题。"
    "请校对并输出：\n"
    "1. 修正明显的错别字、同音误字、错误断句；\n"
    "2. 补充缺失的标点（以句号、逗号为主）；\n"
    "3. 删除无意义的口语填充词（如“然后、就是说、呃、嗯”等），但保留原意和语气；\n"
    "4. 不删减内容、不添加改写原意、不合并段落；\n"
    "5. 保留原文的段落结构，段落之间空一行；\n"
    "6. 直接输出校对后的文本，不要任何解释、前言或额外标记。"
)


def _proofread_chunks(client: OpenAI, model: str, system: str, raw_text: str,
                      progress: Optional[Callable[[float], None]] = None,
                      on_status: Optional[Callable[[str], None]] = None,
                      cancel: Optional[threading.Event] = None,
                      workers: int = 1) -> str:
    chunks = split_chunks(raw_text, CHUNK_LIMIT)
    n = len(chunks)

    def work(chunk: str) -> str:
        _check_cancel(cancel)
        in_len = max(1, len(chunk))
        out = _chat_stream_client(client, model, system, chunk,
                                  max_tokens=min(4096, max(512, int(in_len * 1.5))),
                                  cancel=cancel)
        return out or chunk  # 空结果回退原文

    if workers <= 1 or n == 1:
        done: list[str] = []
        for i, chunk in enumerate(chunks):
            _check_cancel(cancel)
            if on_status and n > 1:
                on_status(f"AI 校对中…（第 {i + 1}/{n} 段）")
            in_len = max(1, len(chunk))
            received = 0

            def on_delta(delta: str, _i: int = i) -> None:
                nonlocal received
                received += len(delta)
                if progress:
                    progress((_i + min(1.0, received / in_len)) / n)

            out = _chat_stream_client(client, model, system, chunk,
                                      max_tokens=min(4096, max(512, int(in_len * 1.5))),
                                      on_delta=on_delta, cancel=cancel)
            done.append(out or chunk)
            if progress:
                progress((i + 1) / n)
        return "\n\n".join(done)

    # 并发：分块互不依赖，按原顺序拼回；进度/状态回调只在调用线程里触发
    results: list[Optional[str]] = [None] * n
    finished = 0
    with ThreadPoolExecutor(max_workers=min(workers, n)) as pool:
        futures = {pool.submit(work, chunk): i for i, chunk in enumerate(chunks)}
        for fut in as_completed(futures):
            _check_cancel(cancel)
            results[futures[fut]] = fut.result()
            finished += 1
            if on_status:
                on_status(f"AI 校对中…（已完成 {finished}/{n} 段）")
            if progress:
                progress(finished / n)
    return "\n\n".join(r if r is not None else chunks[i] for i, r in enumerate(results))


def _with_glossary(system: str, settings: Settings) -> str:
    """把专有名词追加到校对提示词，避免模型把术语改成同音字。"""
    hint = glossary.build_glossary_hint(
        glossary.terms_from_settings(settings.glossary_terms))
    return system + hint if hint else system


def _proofread_workers(settings: Settings) -> int:
    """云端 API 有独立算力，分块并发能成倍缩短校对；本地推理并发只会互相拖慢。"""
    return PROOFREAD_MAX_WORKERS if settings.llm_provider == "api" else 1


def _apply_replace_rules(settings: Settings, text: str) -> str:
    """校对后再兜一次替换词典：提示词约束很软，模型常把术语“改回”同音字。

    字面规则幂等；正则规则若替换结果再次匹配源模式会被重复替换（设置页已提示）。
    """
    rules = glossary.rules_from_settings(settings.replace_rules)
    if not rules or not text:
        return text
    return glossary.apply_rules(text, rules)


def proofread(settings: Settings, raw_text: str,
              progress: Optional[Callable[[float], None]] = None,
              on_status: Optional[Callable[[str], None]] = None,
              cancel: Optional[threading.Event] = None) -> str:
    model = settings.llm_model()
    if settings.llm_provider == "llamacpp":
        llm_server.ensure_running(settings)
        model = llm_server.model_id(settings)
    out = _proofread_chunks(_client(settings), model, _with_glossary(PROOF_SYSTEM, settings),
                            raw_text, progress, on_status, cancel,
                            workers=_proofread_workers(settings))
    return _apply_replace_rules(settings, out)


FINE_PROOF_SYSTEM = (
    "你是顶级中文文字校对专家，擅长发现语音转写中的同音/近音错字。"
    "用户会给你一段 B站 视频的中文文稿（可能已经过初步校对，仍残留少量错误）。\n"
    "请精校并输出：\n"
    "1. 结合上下文与常识修正所有错别字，尤其是同音字（如“湖/壶、在/再、做/作、的/地/得、副/付、账/帐”等）；\n"
    "2. 补充缺失标点（以句号、逗号为主），修正明显断句错误；\n"
    "3. 删除无意义口语填充词（“然后、就是说、呃、嗯、那个”等），保留原意和语气；\n"
    "4. 不删减内容、不改写原意、不合并段落，保留段落结构（段落之间空一行）；\n"
    "5. 直接输出精校后的全文，不要任何解释、前言或额外标记。"
)


def fineproofread(settings: Settings, raw_text: str,
                  progress: Optional[Callable[[float], None]] = None,
                  on_status: Optional[Callable[[str], None]] = None,
                  cancel: Optional[threading.Event] = None) -> str:
    """精校：用云端 API 做二遍校对（修正本地小模型漏掉的同音错字）。"""
    base = settings.fine_base.strip()
    model = settings.fine_model.strip()
    if not base or not model:
        raise LLMError("精校未配置：请到 设置 → 精校（云端 API） 填写 API 地址与模型名")
    client = OpenAI(base_url=base.rstrip("/"), api_key=settings.fine_key.strip() or "sk-xxx",
                    timeout=settings.llm_timeout)
    return _apply_replace_rules(settings, _proofread_chunks(
        client, model, _with_glossary(FINE_PROOF_SYSTEM, settings), raw_text,
        progress, on_status, cancel, workers=PROOFREAD_MAX_WORKERS))


CHAPTER_SYSTEM = (
    "你是文本结构分析师。用户会给你一篇校对后的中文文稿（B站视频的文字稿）。\n"
    "请按内容主题划分为 3-10 个章节，每章只输出标题和本章的开头片段：\n"
    "### 章节标题\n"
    "@ 本章开头的前 20-30 个字\n"
    "\n"
    "硬性要求：\n"
    "1. “@”后面必须从原文逐字复制（连续的一段），不得改写、不得补标点、不得跨段拼接；\n"
    "2. 第一章的“@”必须是全文开头的前 20-30 个字；\n"
    "3. 各章“@”在原文中按先后顺序出现，且不要重复同一句；\n"
    "4. 章节标题不超过 20 字；\n"
    "5. 只输出上面的结构，不要解释，不要复制整段正文。"
)

CHAPTER_MAX_TOKENS = 4096   # 锚点格式只需几百 token；余量留给不守格式的模型回退到正文
ANCHOR_RE = re.compile(r"^[@＠]\s*(.+)$")
CHAPTER_TITLE_RE = re.compile(r"^#{1,6}\s*(.+)$")


def parse_anchor_chapters(out: str) -> list[tuple[str, str]]:
    """解析 “### 标题 / @ 开头片段” 结构，返回 [(标题, 锚点)]。"""
    items: list[tuple[str, str]] = []
    title = ""
    for raw in (out or "").splitlines():
        line = raw.strip()
        m = CHAPTER_TITLE_RE.match(line)
        if m:
            title = m.group(1).strip()
            continue
        m = ANCHOR_RE.match(line)
        if m and title:
            items.append((title, m.group(1).strip()))
            title = ""
    return items


def parse_echo_chapters(out: str) -> Optional[list[dict]]:
    """旧格式回退：模型直接回显“### 标题 + 正文”。"""
    chapters: list[dict] = []
    cur_title, cur_text = "", []
    for line in (out or "").splitlines():
        m = CHAPTER_TITLE_RE.match(line.strip())
        if m:
            if cur_title:
                chapters.append({"title": cur_title, "text": "\n".join(cur_text).strip()})
            cur_title, cur_text = m.group(1).strip(), []
        elif cur_title:
            cur_text.append(line)
    if cur_title:
        chapters.append({"title": cur_title, "text": "\n".join(cur_text).strip()})
    chapters = [c for c in chapters if c["title"] and c["text"]]
    return chapters or None


def chapterize(settings: Settings, proofread: str,
               progress: Optional[Callable[[float], None]] = None,
               cancel: Optional[threading.Event] = None) -> Optional[list[dict]]:
    if len(proofread) < CHAPTER_MIN_LEN:
        return None
    text = proofread[:CHAPTER_INPUT_LIMIT]
    received = 0

    def on_delta(delta: str):
        nonlocal received
        received += len(delta)
        if progress:
            progress(min(0.98, received / 800))  # 锚点输出很短，按预期长度估进度

    try:
        out = _chat_stream(settings, CHAPTER_SYSTEM, text,
                           max_tokens=CHAPTER_MAX_TOKENS, on_delta=on_delta, cancel=cancel)
    except LLMError:
        return None
    if progress:
        progress(1.0)
    # 首选锚点切分：正文由程序从 proofread 切出，覆盖率 100%，不受输出 token 上限影响。
    # 锚点只可能落在送入模型的前缀里，前缀之后的正文并入最后一章（不丢字，只是不再细分）。
    sliced = slice_by_anchors(proofread, parse_anchor_chapters(out))
    if sliced:
        return sliced
    # 模型没按锚点格式输出：退回回显解析，交给 ensure_chapters_cover 兜底
    return parse_echo_chapters(out)


def summarize(settings: Settings, proofread: str, granularity: str,
              progress: Optional[Callable[[float], None]] = None,
              cancel: Optional[threading.Event] = None) -> Optional[str]:
    if granularity == "off":
        return None
    if granularity == "one_line":
        ask = "请用一句话概括这段内容的核心理念或主题，不超过60字，直接输出。"
    elif granularity == "bullets":
        ask = "请用5-8条要点总结这段内容的要点，每条一行，以“- ”开头，不要输出其他内容。"
    else:
        ask = "请用10-15条要点详尽总结这段内容，每条一行，以“- ”开头，不要输出其他内容。"
    system = "你是擅长提炼要点的中文内容总结助手。"
    if len(proofread) <= SUMMARY_INPUT_LIMIT:
        return _summarize_one(settings, system, ask + "\n\n" + proofread,
                              len(proofread), progress, cancel)
    # 长文本 map-reduce：分块各自总结再合并，避免只总结开头
    chunks = split_chunks(proofread, SUMMARY_INPUT_LIMIT)
    partials: list[str] = []
    for i, chunk in enumerate(chunks):
        _check_cancel(cancel)
        if progress:
            progress(0.98 * i / len(chunks))
        partials.append(_summarize_one(
            settings, system, "请提炼这段内容的全部要点，每条一行，以“- ”开头，不要输出其他内容。\n\n" + chunk,
            len(chunk), None, cancel))
    merged = "\n".join(partials)
    if progress:
        progress(0.99)
    note = "（注：全文较长，以下为分段归纳后合并的总结）\n" if len(chunks) > 1 else ""
    return note + _summarize_one(settings, system,
                                 ask + "\n\n以下是各部分的小结，请合并为整体总结：\n" + merged,
                                 len(merged), None, cancel)


def _summarize_one(settings: Settings, system: str, user: str, in_len: int,
                   progress: Optional[Callable[[float], None]],
                   cancel: Optional[threading.Event] = None) -> str:
    received = 0

    def on_delta(delta: str):
        nonlocal received
        received += len(delta)
        if progress:
            progress(min(0.98, received / max(1, in_len)))

    return _chat_stream(settings, system, user,
                        max_tokens=2048, on_delta=on_delta, cancel=cancel)

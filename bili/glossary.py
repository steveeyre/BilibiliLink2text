"""专有名词与替换词典（Vocotype 式）。

两条互相独立的链路：
  * 专有名词 terms —— 作为 whisper.cpp 的 initial prompt 喂给 ASR，同时写进校对提示词，
    要求模型原样保留这些写法；
  * 替换词典 rules —— 转写/字幕出来后、合并段落之前做的确定性替换。专有名词交给模型
    容易猜成同音字，这类固定映射放在规则里更可靠。

规则按书写顺序依次生效（先写的先替换），字面规则走 str.replace（无正则转义问题），
正则规则允许 \\1 反向引用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

# 提示词里术语总长度上限：whisper 的 initial prompt 只吃 224 token 左右，
# 超长会被静默截断，反而不如少给几个。
ASR_PROMPT_LIMIT = 180
# 校对提示词可以宽松一些，但也不该挤占正文预算。
GLOSSARY_HINT_LIMIT = 400

_SEPARATORS = ("=>", "->", "→")


class GlossaryError(ValueError):
    """规则文本无法解析（带行号，便于设置页直接提示）。"""


@dataclass(frozen=True)
class Rule:
    src: str
    dst: str
    regex: bool = False

    def to_dict(self) -> dict:
        return {"from": self.src, "to": self.dst, "regex": self.regex}

    def as_line(self) -> str:
        body = f"/{self.src}/" if self.regex else self.src
        return f"{body} => {self.dst}"


def _strip_comment(line: str) -> str:
    return line.strip()


def _split_line(line: str) -> tuple[str, str]:
    for sep in _SEPARATORS:
        if sep in line:
            src, dst = line.split(sep, 1)
            return src.strip(), dst.strip()
    raise GlossaryError("缺少分隔符")


def parse_rules(text: str) -> list[Rule]:
    """把设置页的多行文本解析为规则；空行与 # 注释忽略。"""
    rules: list[Rule] = []
    for lineno, raw in enumerate((text or "").splitlines(), start=1):
        line = _strip_comment(raw)
        if not line or line.startswith("#"):
            continue
        try:
            src, dst = _split_line(line)
        except GlossaryError:
            raise GlossaryError(f"第 {lineno} 行：缺少 “=>”，格式应为 错误词 => 正确词") from None
        if not src:
            raise GlossaryError(f"第 {lineno} 行：左侧为空")
        regex = False
        if len(src) >= 2 and src.startswith("/") and src.endswith("/"):
            src = src[1:-1]
            regex = True
            if not src:
                raise GlossaryError(f"第 {lineno} 行：正则内容为空")
            try:
                re.compile(src)
            except re.error as e:
                raise GlossaryError(f"第 {lineno} 行：正则非法（{e}）") from None
        rules.append(Rule(src=src, dst=dst, regex=regex))
    return rules


def format_rules(rules: Iterable[Rule]) -> str:
    return "\n".join(r.as_line() for r in rules)


def parse_terms(text: str) -> list[str]:
    """专有名词：每行一个，去重并保持顺序。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in (text or "").splitlines():
        term = raw.strip()
        if not term or term.startswith("#") or term in seen:
            continue
        seen.add(term)
        out.append(term)
    return out


def format_terms(terms: Sequence[str]) -> str:
    return "\n".join(terms)


def merge_rules(current: Sequence[Rule], incoming: Sequence[Rule]) -> list[Rule]:
    """按 (源, 是否正则) 去重合并，保留先出现的顺序（导入词典用）。"""
    out = list(current)
    seen = {(r.src, r.regex) for r in out}
    for rule in incoming:
        key = (rule.src, rule.regex)
        if key in seen:
            continue
        seen.add(key)
        out.append(rule)
    return out


def rules_from_settings(raw: object) -> list[Rule]:
    """从 Settings.replace_rules（list[dict] 或历史脏数据）还原规则，坏条目跳过。

    解析在设置页保存时就校验过，运行时不该因为一条坏规则中断整条管线。
    """
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[Rule] = []
    for item in raw:
        if isinstance(item, Rule):
            out.append(item)
            continue
        if isinstance(item, dict):
            src = str(item.get("from") or item.get("src") or "").strip()
            dst = str(item.get("to") or item.get("dst") or "")
            regex = bool(item.get("regex"))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            src, dst = str(item[0]).strip(), str(item[1])
            regex = False
        else:
            continue
        if not src:
            continue
        if regex:
            try:
                re.compile(src)
            except re.error:
                continue
        out.append(Rule(src=src, dst=dst, regex=regex))
    return out


def terms_from_settings(raw: object) -> list[str]:
    if isinstance(raw, str):
        return parse_terms(raw)
    if not isinstance(raw, (list, tuple)):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        term = str(item).strip()
        if term and term not in seen:
            seen.add(term)
            out.append(term)
    return out


def apply_rules(text: str, rules: Sequence[Rule]) -> str:
    if not text:
        return text
    for rule in rules:
        if not rule.src:
            continue
        if rule.regex:
            try:
                text = re.sub(rule.src, rule.dst, text)
            except re.error:
                continue
        else:
            text = text.replace(rule.src, rule.dst)
    return text


def apply_rules_to_segments(segments: Sequence[dict], rules: Sequence[Rule]) -> list[dict]:
    """按规则改写每段文本，时间戳保持不变；替换后变空的段丢弃。"""
    if not rules:
        return list(segments)
    out: list[dict] = []
    for seg in segments:
        text = apply_rules(seg.get("text") or "", rules)
        if not text.strip():
            continue
        item = dict(seg)
        item["text"] = text
        out.append(item)
    return out


def _join_limited(items: Sequence[str], limit: int) -> str:
    pieces: list[str] = []
    used = 0
    for item in items:
        cost = len(item) + 1
        if pieces and used + cost > limit:
            break
        pieces.append(item)
        used += cost
    return "、".join(pieces)


def build_asr_prompt(terms: Sequence[str], limit: int = ASR_PROMPT_LIMIT) -> str:
    """whisper.cpp --prompt 用的初始提示；没有术语时返回空串。"""
    body = _join_limited([t for t in terms if t], limit)
    if not body:
        return ""
    return f"本视频会提到这些专有名词：{body}。"


def build_glossary_hint(terms: Sequence[str], limit: int = GLOSSARY_HINT_LIMIT) -> str:
    """校对提示词追加段落；没有术语时返回空串。"""
    body = _join_limited([t for t in terms if t], limit)
    if not body:
        return ""
    return (f"\n\n本视频的专有名词：{body}。\n"
            "这些词必须原样保留，不要改写成同音字或近义词；"
            "若原文中出现了它们的明显错写，请按上面的写法修正。")

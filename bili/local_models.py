"""发现本机已有大模型（.ollama/models / GGUF / Ollama 注册表），不触发下载。"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class LocalModel:
    """一款可直接调用的本地模型。"""

    key: str            # 内部唯一键
    name: str           # 展示名
    source: str         # "gguf" | "ollama"
    path: str           # GGUF 绝对路径，或 Ollama 模型名
    size_gb: float
    hint: str = ""
    family: str = ""    # 目录名 / 模型族，便于区分

    @property
    def display(self) -> str:
        size = f"{self.size_gb:.1f}GB" if self.size_gb > 0 else "?"
        extra = f" · {self.hint}" if self.hint else ""
        return f"{self.name}  [{size}]{extra}"


def ollama_models_root() -> Optional[Path]:
    """优先 OLLAMA_MODELS，其次 %USERPROFILE%\\.ollama\\models。"""
    candidates = [
        Path(os.environ["OLLAMA_MODELS"]) if os.environ.get("OLLAMA_MODELS") else None,
        Path.home() / ".ollama" / "models",
    ]
    for p in candidates:
        if p and p.is_dir():
            return p
    return None


def _size_gb(p: Path) -> float:
    try:
        return p.stat().st_size / 1e9
    except OSError:
        return 0.0


def _scan_gguf_tree(root: Path) -> list[LocalModel]:
    """扫描 root 下的 *.gguf（跳过 ollama blobs/manifests）。"""
    out: list[LocalModel] = []
    skip = {"blobs", "manifests"}
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return out

    for entry in entries:
        if entry.name.lower() in skip or entry.name.startswith("."):
            continue
        if entry.is_file() and entry.suffix.lower() == ".gguf":
            out.append(
                LocalModel(
                    key=f"gguf:{entry}",
                    name=entry.stem,
                    source="gguf",
                    path=str(entry),
                    size_gb=_size_gb(entry),
                    hint="GGUF · llama.cpp",
                    family=root.name,
                )
            )
            continue
        if not entry.is_dir():
            continue
        # 子目录：取主模型 GGUF（排除 mmproj / draft / mtp 等附属文件）
        ggufs = sorted(entry.glob("*.gguf"), key=lambda p: p.stat().st_size, reverse=True)
        main = None
        for g in ggufs:
            low = g.name.lower()
            if "mmproj" in low or "mtp" in low or "draft" in low:
                continue
            main = g
            break
        if main is None and ggufs:
            main = ggufs[0]
        if main is None:
            continue
        out.append(
            LocalModel(
                key=f"gguf:{main}",
                name=entry.name,
                source="gguf",
                path=str(main),
                size_gb=_size_gb(main),
                hint="GGUF · llama.cpp",
                family=entry.name,
            )
        )
    return out


def _scan_ollama_api(timeout: float = 1.2) -> list[LocalModel]:
    """若 Ollama 服务在跑，列出已注册模型名。"""
    url = "http://127.0.0.1:11434/api/tags"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BiliLink2Text"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", errors="ignore"))
    except Exception:
        return []
    out: list[LocalModel] = []
    for item in data.get("models") or []:
        name = item.get("name") or item.get("model") or ""
        if not name:
            continue
        size = (item.get("size") or 0) / 1e9
        out.append(
            LocalModel(
                key=f"ollama:{name}",
                name=name,
                source="ollama",
                path=name,
                size_gb=size,
                hint="Ollama",
                family=name.split("/")[0],
            )
        )
    return out


def _norm_model_name(name: str) -> str:
    n = (name or "").strip().lower().replace("_", "-")
    if n.endswith(":latest"):
        n = n[: -len(":latest")]
    # tobestyledintro/qwen3.8-9b-distill 与 qwen3.8-9b-distill 视为同一模型
    if "/" in n:
        n = n.split("/")[-1]
    return n


def scan_local_models(extra_dirs: Optional[list[Path]] = None) -> list[LocalModel]:
    """汇总本机可用 Ollama 模型（只保留 Ollama，按规范化名去重）。"""
    found: list[LocalModel] = []
    seen: set[str] = set()

    def add(m: LocalModel) -> None:
        key = _norm_model_name(m.path or m.name)
        if not key or key in seen:
            return
        seen.add(key)
        found.append(m)

    # 优先 Ollama 注册表（名字更准确）
    for m in _scan_ollama_api():
        add(m)

    # 兜底：.ollama/models 目录名（未在注册表时）
    roots: list[Path] = []
    r = ollama_models_root()
    if r:
        roots.append(r)
    if extra_dirs:
        roots.extend(extra_dirs)
    for root in roots:
        for m in _scan_gguf_tree(root):
            add(
                LocalModel(
                    key=f"ollama:{m.name}",
                    name=m.name,
                    source="ollama",
                    path=m.name,
                    size_gb=m.size_gb,
                    hint="Ollama",
                    family=m.family or m.name,
                )
            )

    found.sort(key=lambda m: (-m.size_gb, m.name.lower()))
    return found


def pick_default(models: list[LocalModel]) -> Optional[LocalModel]:
    """默认优先：qwen3.8-9b-distill / 名字含 9b、distill 的中等体积模型。"""
    if not models:
        return None
    want = _norm_model_name("qwen3.8-9b-distill")
    for m in models:
        if _norm_model_name(m.path) == want or _norm_model_name(m.name) == want:
            return m
    preferred = [
        m for m in models
        if any(k in (m.name + m.path).lower() for k in ("9b", "distill", "qwen3.8"))
        and 3.0 <= m.size_gb <= 10.0
    ]
    if preferred:
        return preferred[0]
    return models[0]


def apply_local_model(settings, model: LocalModel) -> None:
    """把选中的模型写入 Settings（仅 Ollama）。"""
    settings.llm_provider = "ollama"
    settings.ollama_model = model.path


def ollama_model_choices(models: Optional[list[LocalModel]] = None) -> list[str]:
    """Ollama 模型下拉候选：注册表/检测到的模型名 + 默认模型。"""
    if models is None:
        models = scan_local_models()
    names: list[str] = []
    seen: set[str] = set()

    def add(n: str) -> None:
        n = (n or "").strip()
        key = _norm_model_name(n)
        if not n or key in seen:
            return
        seen.add(key)
        names.append(n)

    for m in models:
        add(m.path)
    return names

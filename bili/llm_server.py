"""llama.cpp (Vulkan) 本地大模型服务：按需启动 / 健康检查 / 退出停止。

Windows 下 Ollama 的 ROCm 仅支持 Linux（核显只能回退 CPU），而 llama.cpp
的 Vulkan 后端可直接调用 AMD 核显（Radeon 780M）。本模块负责在
AI 校对/总结/切章前按需拉起 llama-server.exe 并等待就绪。
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Optional

from .config import Settings, app_dir, cache_dir
from .local_models import ollama_models_root

DEFAULT_EXE = app_dir() / "llama-cpp" / "llama-server.exe"
DEFAULT_MODELS = app_dir() / "models" / "llm"

_state: dict = {"proc": None, "model_id": None, "base_url": None}


class LlamaServerError(Exception):
    pass


def exe_path(s: Settings) -> Path:
    if s.llamacpp_exe.strip():
        return Path(s.llamacpp_exe).expanduser()
    return DEFAULT_EXE


def _largest_gguf_in(root: Path) -> Optional[Path]:
    if not root.is_dir():
        return None
    ggs = sorted(root.glob("*.gguf"), key=lambda p: p.stat().st_size, reverse=True)
    return ggs[0] if ggs else None


def model_path(s: Settings) -> Path:
    v = s.llamacpp_model.strip()
    if v:
        p = Path(v).expanduser()
        # 只填了文件名时，视为 models/llm 目录下的文件（便携，不写死绝对路径）
        if not p.is_absolute() and p.suffix.lower() == ".gguf":
            p = DEFAULT_MODELS / p.name
            if not p.is_file():
                root = ollama_models_root()
                if root:
                    hits = list(root.rglob(p.name))
                    if hits:
                        return hits[0]
        return p
    hit = _largest_gguf_in(DEFAULT_MODELS)
    if hit:
        return hit
    root = ollama_models_root()
    if root:
        ggs = sorted(root.rglob("*.gguf"), key=lambda p: p.stat().st_size, reverse=True)
        for g in ggs:
            low = g.name.lower()
            if "mmproj" in low or "mtp" in low:
                continue
            return g
    raise LlamaServerError(
        f"未找到 GGUF 模型。请在 设置→大模型 指定模型文件"
        f"（默认扫描：{DEFAULT_MODELS} 与 .ollama/models）"
    )


def _http_get(url: str, timeout: float = 3.0) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BiliLink2Text"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="ignore")
    except Exception:
        return None


def is_up(s: Settings) -> bool:
    body = _http_get(f"http://127.0.0.1:{s.llamacpp_port}/health", 2.0)
    if not body:
        return False
    text = body.strip().lower()
    # llama-server /health 返回 JSON（{"status":"ok"}），老版本返回裸 "ok"
    return text == "ok" or '"status":"ok"' in text


def _query_model_id(s: Settings) -> str:
    body = _http_get(f"http://127.0.0.1:{s.llamacpp_port}/v1/models", 3.0)
    if body:
        try:
            data = json.loads(body)
            if data.get("data"):
                return data["data"][0]["id"]
        except Exception:
            pass
    return ""


def model_id(s: Settings) -> str:
    if not _state["model_id"] and is_up(s):
        _state["model_id"] = _query_model_id(s)
    return _state["model_id"] or "qwen2.5:7b"


def _kill_proc(proc) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _start_once(s: Settings, ngl: int, wait: int, log: Path):
    """启动一次 llama-server；就绪返回 base 地址，失败抛 LlamaServerError。"""
    exe = exe_path(s)
    base = f"http://127.0.0.1:{s.llamacpp_port}/v1"
    flags = [
        str(exe),
        "-m", str(model_path(s)),
        "--port", str(s.llamacpp_port),
        "--host", "127.0.0.1",
        "-ngl", str(ngl),
        "-t", str(s.llamacpp_threads),
        "--ctx-size", str(s.llamacpp_ctx),
        "--parallel", "1",          # 队列串行处理，单槽位省显存
        "--jinja",
    ]
    if s.llamacpp_kv_q8:
        # q8_0 量化 KV 缓存省约一半显存；质量损失极小，核显 8K 上下文必需
        flags += ["--cache-type-k", "q8_0", "--cache-type-v", "q8_0"]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with open(log, "ab", buffering=0) as logf:
        try:
            proc = subprocess.Popen(
                flags,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=logf,
                creationflags=creationflags,
            )
        except OSError as e:
            raise LlamaServerError(f"启动 llama-server 失败：{e}") from e
    deadline = time.time() + wait
    while time.time() < deadline:
        if proc.poll() is not None:
            raise LlamaServerError(
                f"llama-server 启动后退出（退出码 {proc.returncode}，-ngl {ngl}），"
                f"请查看日志 {log}"
            )
        if is_up(s):
            return proc
        time.sleep(1)
    _kill_proc(proc)
    raise LlamaServerError(f"llama-server 启动超时（-ngl {ngl}），请查看日志 {log}")


def _ngl_fallbacks(configured: int) -> list[int]:
    """从配置层数起逐级降级，保证小显存机器也能启动。"""
    ladder = [configured, 28, 20, 14, 8, 0]
    seen, out = set(), []
    for n in ladder:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def ensure_running(s: Settings) -> str:
    """确保本地 llama-server 已启动并就绪，返回 OpenAI 兼容 /v1 地址。

    显存不足导致加载失败时自动降低 -ngl 重试，保证任意显卡容量都能启动。
    """
    base = f"http://127.0.0.1:{s.llamacpp_port}/v1"
    if is_up(s):
        return base
    exe = exe_path(s)
    if not exe.is_file():
        raise LlamaServerError(f"未找到 llama-server.exe：{exe}")
    if not model_path(s).is_file():
        raise LlamaServerError(f"GGUF 模型文件不存在：{model_path(s)}")

    log = cache_dir() / "llamacpp.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    last_err: Optional[str] = None
    for ngl in _ngl_fallbacks(s.llamacpp_ngl):
        try:
            proc = _start_once(s, ngl, 120, log)
            _state["proc"] = proc
            _state["base_url"] = base
            _state["model_id"] = _query_model_id(s)
            with open(log, "ab") as f:
                f.write(f"\n[ok] -ngl {ngl} 启动成功\n".encode())
            return base
        except LlamaServerError as e:
            last_err = str(e)
            _kill_proc(_state.get("proc"))
            _state["proc"] = None
            time.sleep(1)
    raise LlamaServerError(
        f"llama-server 全部启动尝试失败，最后错误：{last_err}。"
        f"请查看日志 {log}"
    )


def stop() -> None:
    p = _state["proc"]
    _state["proc"] = None
    _state["model_id"] = None
    _state["base_url"] = None
    if p is not None and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

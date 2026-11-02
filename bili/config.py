"""Settings storage and path helpers.

敏感字段（api_key / fine_key / cookies_string）落盘时经 Windows DPAPI
（CryptProtectData，本机用户绑定）加密；非 Windows 或 DPAPI 不可用时
回退明文并在字段旁标注。load 失败会备份损坏文件而非静默丢弃；
save 采用临时文件 + 原子替换，避免截断写导致“设置全丢”。
"""
from __future__ import annotations

import base64
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .glossary import rules_from_settings, terms_from_settings

APP_NAME = "BiliLink2Text"
# 配置结构版本：字段改名/默认值变更时递增，并补一条迁移函数，
# 否则旧配置会静默丢掉设置项
CONFIG_VERSION = 1


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def app_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def config_file() -> Path:
    return app_dir() / "config.json"


def data_dir() -> Path:
    if is_frozen():
        local = app_dir() / "data"
        try:
            local.mkdir(parents=True, exist_ok=True)
            return local  # 便携版：数据放 exe 旁，可整包带走
        except OSError:
            pass
        d = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
        d.mkdir(parents=True, exist_ok=True)
        return d
    d = app_dir() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def models_dir() -> Path:
    d = data_dir() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def tools_dir() -> Path:
    return app_dir() / "tools"


def cache_dir() -> Path:
    d = data_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_file() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "app.log"


def read_log_tail(max_lines: int = 50, max_bytes: int = 64000) -> str:
    """读日志末尾若干行：窗口化构建没有控制台，诊断面板靠它拿到现场。"""
    f = log_file()
    if not f.exists():
        return ""
    try:
        with open(f, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError as e:
        return f"[日志读取失败] {e}"
    lines = data.decode("utf-8", "replace").splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return "\n".join(lines)


WHISPER_SIZES = ["tiny", "base", "small", "medium", "large-v3"]
SUMMARY_GRANULARITIES = ["off", "one_line", "bullets", "detailed"]
ASR_ENGINES = ["sherpa", "whispercpp"]
LLM_PROVIDERS = ["ollama", "api"]
COOKIE_BROWSERS = ["edge", "chrome", "firefox"]

# 落盘加密的敏感字段（内存中 Settings 仍为明文，用法不变）
SECRET_FIELDS = ("api_key", "fine_key", "cookies_string")
_DPAPI_PREFIX = "DPAPI:"


def _dpapi_protect(plain: str) -> Optional[str]:
    """DPAPI 加密，失败/非 Windows 返回 None（调用方回退明文）。"""
    if not plain or os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_byte))]

        buf = ctypes.create_string_buffer(plain.encode("utf-8"))
        blob_in = DATA_BLOB(len(buf) - 1, ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
        blob_out = DATA_BLOB()
        crypt32 = ctypes.windll.crypt32
        if not crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                        ctypes.byref(blob_out)):
            return None
        try:
            enc = bytes((ctypes.c_byte * blob_out.cbData).from_address(
                ctypes.addressof(blob_out.pbData.contents)))
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return _DPAPI_PREFIX + base64.b64encode(enc).decode()
    except Exception:
        return None


def _dpapi_unprotect(stored: str) -> Optional[str]:
    """DPAPI 解密，失败返回 None（调用方按明文兼容处理）。"""
    if not stored.startswith(_DPAPI_PREFIX) or os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_byte))]

        raw = base64.b64decode(stored[len(_DPAPI_PREFIX):])
        buf = ctypes.create_string_buffer(raw, len(raw))
        blob_in = DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
        blob_out = DATA_BLOB()
        crypt32 = ctypes.windll.crypt32
        if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                          ctypes.byref(blob_out)):
            return None
        try:
            dec = bytes((ctypes.c_byte * blob_out.cbData).from_address(
                ctypes.addressof(blob_out.pbData.contents)))
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return dec.decode("utf-8")
    except Exception:
        return None


def secrets_encrypted() -> bool:
    """当前平台能否做 DPAPI 落盘加密（用于设置界面提示）。"""
    return os.name == "nt"

# 可在线下载的推荐 GGUF 模型（普通主机/核显优先，显存占用从低到高）
# (显示名, 文件名, 下载地址, 大小字节, 显存提示)
LLM_MODEL_OPTIONS = [
    ("Qwen2.5-3B-Instruct · Q4_K_M（最轻）", "Qwen2.5-3B-Instruct-Q4_K_M.gguf",
     "https://hf-mirror.com/bartowski/Qwen2.5-3B-Instruct-GGUF/resolve/main/Qwen2.5-3B-Instruct-Q4_K_M.gguf",
     1929903264, "约 2.5GB 显存 · 核显可用"),
    ("Qwen3-4B · Q4_K_M（轻量）", "Qwen3-4B-Q4_K_M.gguf",
     "https://hf-mirror.com/unsloth/Qwen3-4B-GGUF/resolve/main/Qwen3-4B-Q4_K_M.gguf",
     2497281312, "约 3GB 显存 · 核显友好"),
    ("Qwen2.5-7B-Instruct · Q4_K_M", "Qwen2.5-7B-Instruct-Q4_K_M.gguf",
     "https://hf-mirror.com/bartowski/Qwen2.5-7B-Instruct-GGUF/resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf",
     4683074240, "约 5GB 显存 · 中"),
    ("Qwen3-8B · Q4_K_M（推荐）", "Qwen3-8B-Q4_K_M.gguf",
     "https://hf-mirror.com/unsloth/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf",
     5027784512, "约 5.5GB 显存 · 中（8GB 独显可全量加载）"),
    ("Qwen3-8B · Q8_0（高精度）", "Qwen3-8B-Q8_0.gguf",
     "https://hf-mirror.com/unsloth/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q8_0.gguf",
     8709519168, "约 9.5GB 显存 · 高（适合 12GB 以上独显）"),
]


@dataclass
class Settings:
    # --- ASR ---
    asr_engine: str = "sherpa"                 # sherpa | whispercpp
    whisper_model: str = "medium"              # tiny/base/small/medium/large-v3
    always_asr: bool = False                   # 跳过字幕直接 ASR
    # --- 专有名词 / 替换词典 ---
    glossary_terms: list = field(default_factory=list)   # 专有名词：喂 whisper prompt + 校对提示
    replace_rules: list = field(default_factory=list)    # [{"from","to","regex"}]，转写后确定性替换
    # --- LLM ---
    llm_provider: str = "ollama"               # ollama | llamacpp | api
    ollama_base: str = "http://127.0.0.1:11434/v1"
    ollama_model: str = "qwen2.5:7b"           # 通用默认；实际以 .ollama/models 已安装模型为准
    local_model_picked: bool = False           # 启动时是否已选过 .ollama/models 里的模型
    llamacpp_exe: str = ""                     # 空 = app_dir/llama-cpp/llama-server.exe
    llamacpp_model: str = ""                   # 空 = app_dir/models/llm/ 下最大的 .gguf
    llamacpp_port: int = 8080
    llamacpp_ngl: int = 28                     # GPU 层数（Vulkan）；28=全 transformer 层卸载，本机核显最优速度；显存不足自动降级
    llamacpp_ctx: int = 8192                   # 上下文大小
    llamacpp_threads: int = 8                  # CPU 线程数
    llamacpp_kv_q8: bool = True                # KV 缓存 q8_0 量化，省约一半显存；本机核显 8K 上下文必需
    api_base: str = ""
    api_key: str = ""
    api_model: str = ""
    llm_timeout: int = 600
    # --- 精校（云端 API，可选；独立于主 LLM，用于高质量二遍校对） ---
    fine_base: str = ""
    fine_key: str = ""
    fine_model: str = ""
    # --- AI 总结 ---
    summary_granularity: str = "bullets"       # off | one_line | bullets | detailed
    # --- 导出 ---
    export_enabled: bool = True
    export_raw: bool = True                    # 原文稿
    export_timestamps: bool = False            # 原片跳转时间戳
    export_summary: bool = True                # AI 总结
    export_dir: str = ""                       # 空 = 默认 app_dir/输出
    # --- Cookies / 网络 ---
    cookies_source: str = "none"               # browser | file | string | none
    cookies_browser: str = "edge"
    cookies_file: str = ""
    cookies_string: str = ""
    proxy: str = ""
    config_version: int = CONFIG_VERSION

    def llm_base_url(self) -> str:
        if self.llm_provider == "ollama":
            return self.ollama_base
        if self.llm_provider == "llamacpp":
            return f"http://127.0.0.1:{self.llamacpp_port}/v1"
        return self.api_base

    def llm_api_key(self) -> str:
        if self.llm_provider in ("ollama", "llamacpp"):
            return ""
        return self.api_key

    def llm_model(self) -> str:
        if self.llm_provider == "ollama":
            return self.ollama_model
        if self.llm_provider == "llamacpp":
            # llama-server 不校验模型名；llm.py 会改用 /v1/models 返回的真实 id
            return Path(self.llamacpp_model).stem if self.llamacpp_model.strip() else "local-gguf"
        return self.api_model or "gpt-4o-mini"


def default_settings() -> Settings:
    return Settings()


def _migrate_0_to_1(raw: dict) -> dict:
    """v0 → v1：早期默认模型 qwen3.8-9b-distill 从未真实存在，且被强行注入过下拉框。

    把它改回通用默认值并清掉“已选过模型”标记，让启动时重新探测本机已安装模型。
    """
    if str(raw.get("ollama_model") or "").strip() == "qwen3.8-9b-distill":
        raw["ollama_model"] = Settings.ollama_model
        raw["local_model_picked"] = False
    return raw


_MIGRATIONS = {0: _migrate_0_to_1}


def _backup_config(f: Path, suffix: str) -> None:
    try:
        dst = f.with_suffix(suffix)
        if not dst.exists():
            dst.write_bytes(f.read_bytes())
    except Exception:
        pass


def _migrate(raw: dict, version: int) -> dict:
    """按版本逐级升级；未知的中间版本直接跳到下一已知迁移。"""
    for v in range(max(0, version), CONFIG_VERSION):
        fn = _MIGRATIONS.get(v)
        if fn is not None:
            raw = fn(dict(raw))
    raw["config_version"] = CONFIG_VERSION
    return raw


def load_settings() -> Settings:
    f = config_file()
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            # 损坏的配置不再静默吞掉：备份现场再回默认值
            try:
                bad = f.with_suffix(".corrupt.json")
                if not bad.exists():
                    bad.write_bytes(f.read_bytes())
            except Exception:
                pass
            corrupt_path = config_file()
            print(f"[config] 配置文件损坏，已回退默认值: {corrupt_path} ({e})",
                  file=sys.stderr)
            return default_settings()
        try:
            raw = {k: v for k, v in data.items() if k in Settings.__dataclass_fields__}
            try:
                version = int(data.get("config_version") or 0)
            except (TypeError, ValueError):
                version = 0
            if version > CONFIG_VERSION:
                # 新版本写的配置：先备份，避免本版本回存时静默删掉未知字段
                _backup_config(f, ".bak.json")
                version = CONFIG_VERSION
            migrated = version != CONFIG_VERSION
            raw = _migrate(raw, version)
            legacy_plaintext = False
            for k in SECRET_FIELDS:
                v = raw.get(k)
                if isinstance(v, str) and v.startswith(_DPAPI_PREFIX):
                    dec = _dpapi_unprotect(v)
                    raw[k] = dec if dec is not None else ""
                elif isinstance(v, str) and v.strip():
                    # 旧版本留下的明文密钥：读出来，稍后原样重写为 DPAPI 密文
                    legacy_plaintext = True
            s = Settings(**raw)
            # 手改/旧版留下的脏数据不该把整条管线带崩：归一成合法结构
            s.glossary_terms = terms_from_settings(s.glossary_terms)
            s.replace_rules = [r.to_dict() for r in rules_from_settings(s.replace_rules)]
        except Exception as e:
            print(f"[config] 配置字段非法，已回退默认值 ({e})", file=sys.stderr)
            return default_settings()
        if migrated or (legacy_plaintext and secrets_encrypted()):
            try:
                save_settings(s)  # 迁移结果 / 明文密钥升级后落盘
            except Exception as e:
                print(f"[config] 配置回写失败: {e}", file=sys.stderr)
        return s
    return default_settings()


def save_settings(s: Settings) -> None:
    data = asdict(s)
    for k in SECRET_FIELDS:
        v = data.get(k)
        if isinstance(v, str) and v:
            enc = _dpapi_protect(v)
            if enc is not None:
                data[k] = enc
    f = config_file()
    tmp = f.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(f)  # 原子替换：崩溃也不会留下截断的 config.json


def resolve_export_dir(s: Settings) -> Path:
    if s.export_dir.strip():
        p = Path(s.export_dir.strip())
        if not p.is_absolute():
            p = app_dir() / p
    else:
        p = app_dir() / "输出"
    p.mkdir(parents=True, exist_ok=True)
    return p

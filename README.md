# BiliLink2Text v1.1

把 B 站视频转成**校对过的文字稿**：字幕 / 本地语音转写 → AI 校对 → 章节化 → 总结 → 导出 Markdown。
Windows 桌面应用（PySide6），模型可全部本地运行，也可接任意 OpenAI 兼容 API。

许可证：MIT　仓库：<https://github.com/steveeyre/BilibiliLink2text>

<p align="center"><img src="assets/icon.png" width="120" alt="icon"></p>

## 功能

- **批量队列**：粘贴多条链接（自动从带标题的文本里提取），按 `bvid + p` 去重；可从「稍后再看 / 收藏夹」扫描入队
- **两条转写路径**：优先使用 B 站自带字幕；无字幕时本地转写（SenseVoice / whisper.cpp），支持中途停止
- **多种 LLM**：Ollama、llama.cpp（本地 GGUF）、OpenAI 兼容 API；分块校对在云端 API 下 3 路并发
- **章节与总结**：章节用「锚点切分」，正文由程序从校对稿切出，**结构上不会丢字**；长文稿按窗口多轮取锚点，最后一段也会分段而不是全并入末章；长文总结走 map-reduce 而非只读开头
- **专有名词 + 替换词典**：术语表喂给 whisper 初始提示与校对提示词，并用带调拼音做同音纠错（补 SenseVoice 不支持热词）；替换规则支持正则、可导入导出；结果区选中错词可右键一键加词
- **精校（可选）**：独立云 API 做二遍校对，可撤销，精校后自动重算章节 / 总结并重新导出
- **可观测**：队列与结果持久化；每个任务有「诊断」面板（阶段耗时、错误原文、日志尾部、一键复制）
- **安全**：API Key / Cookies 落盘前加密——Windows 用 DPAPI，其它平台优先系统钥匙串，其次本地密钥文件；配置原子写入，损坏自动备份

## 环境要求

- Windows 10/11（x64）为主；Linux / macOS 可源代码运行（密钥存储用 keyring 或本地密钥文件）
- Python 3.12（3.10+ 理论可用）
- 可选：Ollama，或本地 GGUF 模型；否则使用云端 API

## 安装运行

```powershell
git clone https://github.com/steveeyre/BilibiliLink2text.git
cd BilibiliLink2text
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

首次运行会自动下载所需模型（默认 SenseVoice，约 230 MB，走 hf-mirror / HuggingFace 镜像）；
ffmpeg 缺失时也会自动下载。全部完成后可离线使用。

## 首次配置

打开「设置」：

| 场景 | 选择 | 需要填写 |
| --- | --- | --- |
| 本地 Ollama | Ollama（本地） | 地址（默认 `http://127.0.0.1:11434/v1`）、模型名（下拉里挑本机已装模型） |
| 本地 GGUF | llama.cpp · 本地 GGUF | `llama-server.exe` 与 `.gguf` 路径（留空则用程序目录下默认位置） |
| 云端 | OpenAI 兼容 API | 地址（如 `https://api.deepseek.com/v1`）、Key、模型名 |

填好后点「测试连接」。需要登录才能观看的视频，再到「Cookies」选择浏览器或 `cookies.txt`。

## 使用

1. 顶部粘贴链接（每行一条，或直接粘带标题的整段文字），点「添加」
2. 「开始处理」；可随时「停止」（下载、转码、转写、校对、章节、总结各阶段都响应）
3. 右侧查看「AI 总结 / 校对文稿」；可复制 Markdown、导出 `.md`、精校、把错词加入替换词典
4. 队列和结果会保存，重启后可直接查看或重新导出

## 目录结构

```
main.py                 入口（--selftest 无头自检）
bili/
  pipeline.py           单视频处理管线（字幕 → 下载/ASR → 替换规则 → 校对 → 章节 → 总结）
  llm.py                校对 / 章节 / 总结，分块与并发、取消
  glossary.py           专有名词与替换词典
  textutil.py           段落合并、章节锚点切分、Markdown 导出
  downloader.py         yt-dlp + ffmpeg（可取消）
  asr_sherpa.py         SenseVoice（sherpa-onnx）+ 静音切分
  asr_whispercpp.py     whisper.cpp
  cookies.py            浏览器 / 文件 / 字符串 Cookie 读取与缓存
  config.py             设置、DPAPI 加密、版本迁移、日志路径
  store.py              队列与结果持久化
  gui/                  主窗口、设置、扫描、诊断、主题与控件
tests/                  单元测试（标准库 unittest）
BiliLink2Text.spec      PyInstaller 打包配置（onedir）
```

本地数据（首次运行生成，已被 `.gitignore` 排除）：

```
config.json     设置（敏感字段加密）
data/cache      音频中间文件，任务成功后自动清理
data/logs       app.log
输出/            导出的 .md
```

不入库的大件：`sherpa-onnx/`（SenseVoice 模型）、`tools/`（ffmpeg、whisper-cli）、`models/`（GGUF）。首次运行会自动下载默认所需的部分，其余按「构建便携版」一节手动放置。

## 测试

```powershell
python -m unittest discover -s tests
```

覆盖文本处理、配置加密与迁移、缓存、下载器取消、LLM 分块 / map-reduce / 章节切分、专有名词同音纠错、诊断信息等，共 138 项。

## 构建便携版

```powershell
pip install pyinstaller
pyinstaller BiliLink2Text.spec --noconfirm --clean
```

`dist\BiliLink2Text` 为 onedir 产物。要得到离线可用的「硬盘版」，再把以下内容放到 exe 同级目录：

- `sherpa-onnx\`（SenseVoice 模型：`model.int8.onnx` + `tokens.txt` + `silero_vad.onnx`）
- `tools\ffmpeg.exe`
- 可选：`tools\whisper-cli\`（whisper.cpp 备用引擎）

自检：`BiliLink2Text.exe --selftest`，结果写入 `%TEMP%\bililink_selftest.txt`，退出码 0 为通过。

## 隐私说明

- 所有处理都在本机完成；只有你主动配置的 LLM / 精校 API 会收到文稿内容
- `config.json` 中的 `api_key` / `fine_key` / `cookies_string` 均加密存储：Windows 用 DPAPI（仅当前用户可解），其它平台优先 keyring（系统钥匙串），否则用 `data/secret.key` 做本地加密（非 Windows 且两者皆不可用时才退化为明文，设置页会标明）
- 请勿把 `config.json`、`data/`（含 `secret.key`）提交到仓库或分享出去

## 已知限制

- 极长文稿（超过 20 个章节窗口，约 24 万字）时，剩余正文仍会并入最后一章；正常视频远达不到
- 同音纠错依赖拼音（pypinyin），多音字按常用读音判断；只处理“等长且仅一个字同音同调”的写法，不会大幅改写
- 带调拼音对英文/数字术语无效（这类词请用替换词典）；可在设置里关闭同音纠错
- 正则替换只在校对前执行一次（保证可重复执行），校对后只补字面规则
- 非 Windows 未安装 keyring 与 cryptography 时，敏感字段退化为明文（设置页会显示“明文”）

## 许可证

本项目采用 [MIT 许可证](LICENSE)。© 2026 steveeyre

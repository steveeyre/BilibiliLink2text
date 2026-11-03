"""设置对话框。"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog,
                               QFormLayout, QFrame, QHBoxLayout, QGroupBox,
                               QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QSpinBox,
                               QVBoxLayout, QWidget)

from .. import glossary
from ..config import (COOKIE_BROWSERS, SUMMARY_GRANULARITIES,
                      WHISPER_SIZES, Settings, save_settings,
                      secret_storage_label, secrets_encrypted)
from ..local_models import ollama_model_choices


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("设置")
        self.setMinimumWidth(520)
        self.resize(560, 640)
        self._build()
        self._load()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        # 内容放入滚动区，屏幕矮时也能滚到最底部（底部按钮固定）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(0, 0, 4, 0)
        cl.setSpacing(12)

        # ---- ASR ----
        asr_box = QGroupBox("语音转写 (ASR)")
        f = QFormLayout(asr_box)
        f.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        f.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        f.setHorizontalSpacing(12)
        f.setVerticalSpacing(10)
        self.asr_engine = QComboBox()
        self.asr_engine.addItem("Sherpa-onnx · SenseVoice（推荐，中文质量高）", "sherpa")
        self.asr_engine.addItem("whisper.cpp（可调模型档位）", "whispercpp")
        self.whisper_model = QComboBox()
        for s in WHISPER_SIZES:
            self.whisper_model.addItem(s, s)
        self.always_asr = QCheckBox("始终语音转写（跳过字幕）")
        f.addRow("ASR 引擎", self.asr_engine)
        f.addRow("whisper.cpp 模型档位", self.whisper_model)
        f.addRow("", self.always_asr)
        cl.addWidget(asr_box)

        # ---- 专有名词 / 替换词典 ----
        gl_box = QGroupBox("专有名词 / 替换词典")
        gl = QVBoxLayout(gl_box)
        gl.setContentsMargins(12, 10, 12, 10)
        gl.setSpacing(6)
        terms_label = QLabel("专有名词（每行一个）")
        gl.addWidget(terms_label)
        self.glossary_terms = QPlainTextEdit()
        self.glossary_terms.setPlaceholderText("每行一个，例如：\nTransformer\n昇腾\nB站")
        self.glossary_terms.setFixedHeight(84)
        self.glossary_terms.setToolTip(
            "喂给 whisper.cpp 作为初始提示，并写入 AI 校对提示词，要求原样保留。")
        gl.addWidget(self.glossary_terms)
        self.glossary_fuzzy = QCheckBox("同音纠错（把与专有名词同音同调的错写自动改回）")
        self.glossary_fuzzy.setToolTip(
            "SenseVoice 不支持热词，这里用拼音比对把“升腾”这类同音错写改回“昇腾”。\n"
            "只作用于转写原文；担心误改可以关掉。")
        rules_label = QLabel("替换词典（每行：错误词 => 正确词）")
        gl.addWidget(rules_label)
        self.replace_rules = QPlainTextEdit()
        self.replace_rules.setPlaceholderText(
            "比西 => B站\n/昇腾|升腾/ => 昇腾\n# 以 # 开头为注释")
        self.replace_rules.setFixedHeight(92)
        self.replace_rules.setToolTip(
            "转写完成后按顺序执行一遍（校对后再执行一次字面规则）。时间戳不受影响。\n"
            "字面规则可重复执行；/正则/ => 替换 只在校对前生效，避免反复替换。")
        gl.addWidget(self.replace_rules)
        io_row = QHBoxLayout()
        import_btn = QPushButton("导入…")
        import_btn.setToolTip("从 .txt 合并规则（每行：错误词 => 正确词）")
        import_btn.clicked.connect(self._import_rules)
        export_btn = QPushButton("导出…")
        export_btn.setToolTip("把当前规则另存为 .txt，方便备份或分享")
        export_btn.clicked.connect(self._export_rules)
        io_row.addWidget(import_btn)
        io_row.addWidget(export_btn)
        io_row.addStretch(1)
        gl.addLayout(io_row)
        rules_hint = QLabel("固定写法的错词放进替换词典；容易听错的人名、产品名放进上面的专有名词。")
        rules_hint.setProperty("role", "hint")
        rules_hint.setWordWrap(True)
        gl.addWidget(rules_hint)
        cl.addWidget(gl_box)

        # ---- LLM ----
        llm_box = QGroupBox("AI 校对 / 总结")
        f = QFormLayout(llm_box)
        f.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        f.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        f.setHorizontalSpacing(12)
        f.setVerticalSpacing(10)
        self.llm_form = f
        self.llm_provider = QComboBox()
        self.llm_provider.addItem("Ollama（本地）", "ollama")
        self.llm_provider.addItem("llama.cpp · 本地 GGUF", "llamacpp")
        self.llm_provider.addItem("OpenAI 兼容 API", "api")
        self.ollama_base = QLineEdit()
        self.ollama_base.setPlaceholderText("默认 http://127.0.0.1:11434/v1")

        # Ollama 模型：下拉选择本机已安装模型，也可手动输入
        self.ollama_model = QComboBox()
        self.ollama_model.setEditable(True)
        self.ollama_model.setInsertPolicy(QComboBox.NoInsert)
        self.ollama_model.setCompleter(None)
        self.ollama_model.setMaxVisibleItems(12)
        self.ollama_model.setMinimumWidth(220)
        self.ollama_model.setPlaceholderText("选择或输入 Ollama 模型名")
        self._reload_ollama_models()
        ollama_model_row = QHBoxLayout()
        ollama_model_row.setContentsMargins(0, 0, 0, 0)
        ollama_model_row.setSpacing(6)
        ollama_model_row.addWidget(self.ollama_model, 1)
        self.ollama_model_refresh = QPushButton("刷新")
        self.ollama_model_refresh.setFixedWidth(52)
        self.ollama_model_refresh.setToolTip("重新扫描 Ollama 已安装模型")
        self.ollama_model_refresh.clicked.connect(self._reload_ollama_models)
        ollama_model_row.addWidget(self.ollama_model_refresh)
        self.ollama_model_row = QWidget()
        self.ollama_model_row.setLayout(ollama_model_row)

        self.api_base = QLineEdit()
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        self.api_model = QLineEdit()

        # llama.cpp：由本程序拉起 llama-server 直接跑本地 .gguf
        self.llamacpp_exe = QLineEdit()
        self.llamacpp_exe.setPlaceholderText("留空 = 程序目录/llama-cpp/llama-server.exe")
        exe_row = QHBoxLayout()
        exe_row.setContentsMargins(0, 0, 0, 0)
        exe_row.setSpacing(6)
        exe_row.addWidget(self.llamacpp_exe, 1)
        exe_browse = QPushButton("浏览…")
        exe_browse.clicked.connect(
            lambda: self._pick_file(self.llamacpp_exe, "选择 llama-server", "可执行文件 (*.exe)"))
        exe_row.addWidget(exe_browse)
        self.llamacpp_exe_row = QWidget()
        self.llamacpp_exe_row.setLayout(exe_row)

        self.llamacpp_model = QLineEdit()
        self.llamacpp_model.setPlaceholderText("留空 = 模型目录/llm 下最大的 .gguf")
        mdl_row = QHBoxLayout()
        mdl_row.setContentsMargins(0, 0, 0, 0)
        mdl_row.setSpacing(6)
        mdl_row.addWidget(self.llamacpp_model, 1)
        mdl_browse = QPushButton("浏览…")
        mdl_browse.clicked.connect(
            lambda: self._pick_file(self.llamacpp_model, "选择 GGUF 模型", "GGUF 模型 (*.gguf)"))
        mdl_row.addWidget(mdl_browse)
        self.llamacpp_model_row = QWidget()
        self.llamacpp_model_row.setLayout(mdl_row)

        self.llamacpp_port = QSpinBox()
        self.llamacpp_port.setRange(1024, 65535)
        self.llamacpp_port.setToolTip("llama-server 监听端口，被占用时改这里")
        self.llamacpp_ngl = QSpinBox()
        self.llamacpp_ngl.setRange(0, 200)
        self.llamacpp_ngl.setToolTip("卸载到 GPU 的层数；显存不足会自动逐级降档重试")
        self.llamacpp_ctx = QSpinBox()
        self.llamacpp_ctx.setRange(512, 131072)
        self.llamacpp_ctx.setSingleStep(1024)
        self.llamacpp_ctx.setToolTip("上下文长度，越大越吃显存 / 内存")
        self.llamacpp_threads = QSpinBox()
        self.llamacpp_threads.setRange(1, 64)
        self.llamacpp_threads.setToolTip("CPU 线程数")
        num_row = QHBoxLayout()
        num_row.setContentsMargins(0, 0, 0, 0)
        num_row.setSpacing(6)
        for label, w in (("端口", self.llamacpp_port), ("GPU 层数", self.llamacpp_ngl),
                         ("上下文", self.llamacpp_ctx), ("线程", self.llamacpp_threads)):
            num_row.addWidget(QLabel(label))
            num_row.addWidget(w)
        num_row.addStretch(1)
        self.llamacpp_num_row = QWidget()
        self.llamacpp_num_row.setLayout(num_row)
        self.llamacpp_kv_q8 = QCheckBox("KV cache 量化（8bit，省显存）")

        self.test_btn = QPushButton("测试连接")
        self.test_btn.setToolTip("按当前服务类型探测地址、列出可用模型")
        self.test_btn.clicked.connect(self._test_llm)
        f.addRow("服务", self.llm_provider)
        f.addRow("Ollama 地址", self.ollama_base)
        f.addRow("Ollama 模型", self.ollama_model_row)
        f.addRow("API 地址", self.api_base)
        f.addRow("API Key", self.api_key)
        f.addRow("API 模型", self.api_model)
        f.addRow("llama-server", self.llamacpp_exe_row)
        f.addRow("GGUF 模型", self.llamacpp_model_row)
        f.addRow("启动参数", self.llamacpp_num_row)
        f.addRow("", self.llamacpp_kv_q8)
        f.addRow(self.test_btn)
        if secrets_encrypted():
            enc_tip = QLabel(f"API Key 等敏感信息经 {secret_storage_label()} 加密后写入 config.json")
            enc_tip.setWordWrap(True)
            enc_tip.setProperty("role", "hint")
            f.addRow("", enc_tip)
        self.llm_provider.currentIndexChanged.connect(self._sync_llm_ui)
        cl.addWidget(llm_box)

        # ---- 精校（云端 API） ----
        fine_box = QGroupBox("精校（云端 API，可选）")
        f = QFormLayout(fine_box)
        f.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        f.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        f.setHorizontalSpacing(12)
        f.setVerticalSpacing(10)
        self.fine_base = QLineEdit()
        self.fine_base.setPlaceholderText("如 https://api.deepseek.com/v1 或 https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.fine_key = QLineEdit()
        self.fine_key.setEchoMode(QLineEdit.Password)
        self.fine_model = QLineEdit()
        self.fine_model.setPlaceholderText("如 deepseek-chat / qwen-plus")
        f.addRow("API 地址", self.fine_base)
        f.addRow("API Key", self.fine_key)
        f.addRow("模型名", self.fine_model)
        self.fine_test_btn = QPushButton("测试连接")
        self.fine_test_btn.setToolTip("用填写的地址与 Key 拉取模型列表验证")
        self.fine_test_btn.clicked.connect(self._test_fine)
        f.addRow(self.fine_test_btn)
        tip = QLabel("在主界面点「精校」按钮，用此云端模型对文稿做二遍校对，\n可修正本地小模型漏掉的同音错字（如“阴阳湖→阴阳壶”）。")
        tip.setWordWrap(True)
        tip.setProperty("role", "hint")
        f.addRow("", tip)
        cl.addWidget(fine_box)

        # ---- 总结 ----
        sum_box = QGroupBox("AI 总结")
        f = QFormLayout(sum_box)
        f.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        f.setHorizontalSpacing(12)
        f.setVerticalSpacing(10)
        self.summary_gran = QComboBox()
        for label, val in [("不总结", "off"), ("一句话", "one_line"),
                           ("要点（默认）", "bullets"), ("详细", "detailed")]:
            self.summary_gran.addItem(label, val)
        f.addRow("总结颗粒度", self.summary_gran)
        cl.addWidget(sum_box)

        # ---- 导出 ----
        exp_box = QGroupBox("自动导出 MarkDown")
        f = QFormLayout(exp_box)
        f.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        f.setHorizontalSpacing(12)
        f.setVerticalSpacing(10)
        self.export_enabled = QCheckBox("处理完成后自动导出 .md")
        self.export_raw = QCheckBox("原文稿")
        self.export_ts = QCheckBox("原片跳转时间戳")
        self.export_summary = QCheckBox("AI 总结")
        dir_row = QHBoxLayout()
        self.export_dir = QLineEdit()
        btn = QPushButton("浏览…")
        btn.clicked.connect(self._pick_export_dir)
        dir_row.addWidget(self.export_dir)
        dir_row.addWidget(btn)
        f.addRow(self.export_enabled)
        f.addRow("导出元素", self.export_raw)
        f.addRow("", self.export_ts)
        f.addRow("", self.export_summary)
        f.addRow("默认存储位置", dir_row)
        cl.addWidget(exp_box)

        # ---- Cookies / 网络 ----
        net_box = QGroupBox("Cookies 与网络")
        f = QFormLayout(net_box)
        f.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        f.setHorizontalSpacing(12)
        f.setVerticalSpacing(10)
        self.cookies_source = QComboBox()
        self.cookies_source.addItem("不配置（公开视频可下载）", "none")
        self.cookies_source.addItem("读取浏览器 Cookie", "browser")
        self.cookies_source.addItem("粘贴 Cookie 字符串（推荐）", "string")
        self.cookies_source.addItem("cookies.txt 文件", "file")
        self.cookies_browser = QComboBox()
        for b in COOKIE_BROWSERS:
            self.cookies_browser.addItem(b, b)
        self.cookies_text = QPlainTextEdit()
        self.cookies_text.setPlaceholderText(
            "在浏览器已登录 B 站时：按 F12 → 网络(Network) → 刷新页面 →\n"
            "点击任意 bilibili 请求 → 请求头(Request Headers) →\n"
            "复制 Cookie 那一整行，粘贴到这里")
        self.cookies_text.setMaximumHeight(72)
        cfile_row = QHBoxLayout()
        self.cookies_file = QLineEdit()
        cbtn = QPushButton("浏览…")
        cbtn.clicked.connect(self._pick_cookie_file)
        cfile_row.addWidget(self.cookies_file)
        cfile_row.addWidget(cbtn)
        self.proxy = QLineEdit()
        f.addRow("Cookie 来源", self.cookies_source)
        f.addRow("浏览器", self.cookies_browser)
        f.addRow("Cookie 内容", self.cookies_text)
        f.addRow("cookies.txt", cfile_row)
        if secrets_encrypted():
            ck_tip = QLabel("粘贴的 Cookie 字符串同样经 DPAPI 本机加密存储；\n"
                            "临时 cookies.txt 只在 data/cache 内复用，退出即删。")
            ck_tip.setWordWrap(True)
            ck_tip.setProperty("role", "hint")
            f.addRow("", ck_tip)
        f.addRow("代理 (可选)", self.proxy)
        self.cookies_source.currentIndexChanged.connect(self._sync_cookie_ui)
        cl.addWidget(net_box)

        cl.addStretch(1)
        scroll.setWidget(content)
        lay.addWidget(scroll, 1)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        btns.addStretch(1)
        ok = QPushButton("保存")
        ok.setProperty("role", "primary")
        ok.setMinimumWidth(96)
        ok.clicked.connect(self._on_save)
        cancel = QPushButton("取消")
        cancel.setMinimumWidth(88)
        cancel.clicked.connect(self.reject)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        lay.addLayout(btns)

    def _sync_cookie_ui(self):
        src = self.cookies_source.currentData()
        for w, on in ((self.cookies_browser, src == "browser"),
                      (self.cookies_text, src == "string"),
                      (self.cookies_file, src == "file")):
            w.setVisible(on)

    def _pick_cookie_file(self):
        f, _ = QFileDialog.getOpenFileName(self, "选择 cookies.txt", "", "Cookie 文件 (*.txt)")
        if f:
            self.cookies_file.setText(f)

    def _pick_export_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择导出目录", self.export_dir.text())
        if d:
            self.export_dir.setText(d)

    def _pick_file(self, target: QLineEdit, title: str, flt: str = "所有文件 (*)"):
        f, _ = QFileDialog.getOpenFileName(self, title, target.text(), flt)
        if f:
            target.setText(f)

    @staticmethod
    def _probe(url: str, key: str = "", timeout: float = 6.0) -> tuple[bool, str]:
        """探测 OpenAI 兼容端点，返回 (可用, 描述)。"""
        req = urllib.request.Request(url)
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}：{e.reason}"
        except Exception as e:
            return False, str(e)
        try:
            data = json.loads(body)
        except ValueError:
            return True, "已连通（响应不是 JSON）"
        items = data.get("models") or data.get("data") or []
        names = []
        for it in items if isinstance(items, list) else []:
            if isinstance(it, dict):
                n = it.get("name") or it.get("id") or it.get("model")
                if n:
                    names.append(str(n))
        if names:
            more = f" 等 {len(names)} 个" if len(names) > 5 else ""
            return True, f"可用模型：{'、'.join(names[:5])}{more}"
        return True, "已连通，但未返回模型列表"

    def _test_llm(self):
        p = self.llm_provider.currentData()
        if p == "ollama":
            root = (self.ollama_base.text().strip() or "http://127.0.0.1:11434/v1").rstrip("/")
            host = root[:-3] if root.endswith("/v1") else root
            ok, msg = self._probe(host + "/api/tags")
            if not ok:
                ok, msg = self._probe(root + "/models")
            if ok and "未返回模型列表" in msg:
                msg = "Ollama 已连通，但未检测到已安装模型，请先 ollama pull"
        elif p == "llamacpp":
            base = f"http://127.0.0.1:{self.llamacpp_port.value()}"
            ok, msg = self._probe(base + "/health")
            if ok:
                ok2, msg2 = self._probe(base + "/v1/models")
                if ok2:
                    msg = msg2
            else:
                msg = "llama-server 尚未运行（保存后处理任务会自动拉起）：" + msg
        else:
            base = self.api_base.text().strip()
            if not base:
                QMessageBox.warning(self, "测试连接", "请先填写 API 地址")
                return
            url = base.rstrip("/")
            if not url.endswith("/v1"):
                url += "/v1"
            ok, msg = self._probe(url + "/models", self.api_key.text().strip())
        if ok:
            QMessageBox.information(self, "测试连接", msg)
        else:
            QMessageBox.warning(self, "测试连接", msg)

    def _test_fine(self):
        base = self.fine_base.text().strip()
        if not base:
            QMessageBox.warning(self, "测试连接", "请先填写精校 API 地址")
            return
        url = base.rstrip("/")
        if not url.endswith("/v1"):
            url += "/v1"
        ok, msg = self._probe(url + "/models", self.fine_key.text().strip())
        if ok:
            QMessageBox.information(self, "测试连接", msg)
        else:
            QMessageBox.warning(self, "测试连接", msg)

    def _reload_ollama_models(self):
        """Ollama 模型下拉：与 installLocalLLM.bat 相同的本机检测结果。"""
        current = self.ollama_model.currentText().strip()
        names = ollama_model_choices()
        self.ollama_model.blockSignals(True)
        self.ollama_model.clear()
        self.ollama_model.addItems(names)
        if current:
            idx = self.ollama_model.findText(current)
            if idx >= 0:
                self.ollama_model.setCurrentIndex(idx)
            else:
                self.ollama_model.setEditText(current)
        elif names:
            self.ollama_model.setCurrentIndex(0)
        self.ollama_model.blockSignals(False)
        self.ollama_model.setPlaceholderText(
            "选择或输入 Ollama 模型名" if names
            else "未检测到已安装模型，请先 ollama pull")

    def _sync_llm_ui(self):
        p = self.llm_provider.currentData()

        def show(w, on):
            w.setVisible(on)
            lbl = self.llm_form.labelForField(w)
            if lbl is not None:
                lbl.setVisible(on)

        for w in (self.ollama_base, self.ollama_model_row):
            show(w, p == "ollama")
        for w in (self.api_base, self.api_key, self.api_model):
            show(w, p == "api")
        for w in (self.llamacpp_exe_row, self.llamacpp_model_row,
                  self.llamacpp_num_row, self.llamacpp_kv_q8):
            show(w, p == "llamacpp")

    def _load(self):
        s = self.settings
        self.asr_engine.setCurrentIndex(self.asr_engine.findData(s.asr_engine))
        self.whisper_model.setCurrentIndex(self.whisper_model.findData(s.whisper_model))
        self.always_asr.setChecked(s.always_asr)
        self.glossary_terms.setPlainText(
            glossary.format_terms(glossary.terms_from_settings(s.glossary_terms)))
        self.glossary_fuzzy.setChecked(getattr(s, "glossary_fuzzy", True))
        self.replace_rules.setPlainText(
            glossary.format_rules(glossary.rules_from_settings(s.replace_rules)))
        provider = s.llm_provider if s.llm_provider in ("ollama", "llamacpp", "api") else "ollama"
        self.llm_provider.setCurrentIndex(self.llm_provider.findData(provider))
        self.ollama_base.setText(s.ollama_base)
        self._reload_ollama_models()
        self.ollama_model.setCurrentText(s.ollama_model)
        self.api_base.setText(s.api_base)
        self.api_key.setText(s.api_key)
        self.api_model.setText(s.api_model)
        self.llamacpp_exe.setText(s.llamacpp_exe)
        self.llamacpp_model.setText(s.llamacpp_model)
        self.llamacpp_port.setValue(int(s.llamacpp_port) or 8080)
        self.llamacpp_ngl.setValue(int(s.llamacpp_ngl))
        self.llamacpp_ctx.setValue(int(s.llamacpp_ctx))
        self.llamacpp_threads.setValue(int(s.llamacpp_threads))
        self.llamacpp_kv_q8.setChecked(s.llamacpp_kv_q8)
        self.fine_base.setText(s.fine_base)
        self.fine_key.setText(s.fine_key)
        self.fine_model.setText(s.fine_model)
        self._sync_llm_ui()
        self.summary_gran.setCurrentIndex(self.summary_gran.findData(s.summary_granularity))
        self.export_enabled.setChecked(s.export_enabled)
        self.export_raw.setChecked(s.export_raw)
        self.export_ts.setChecked(s.export_timestamps)
        self.export_summary.setChecked(s.export_summary)
        self.export_dir.setText(s.export_dir)
        self.cookies_source.setCurrentIndex(self.cookies_source.findData(s.cookies_source))
        self.cookies_browser.setCurrentIndex(self.cookies_browser.findData(s.cookies_browser))
        self.cookies_text.setPlainText(s.cookies_string)
        self.cookies_file.setText(s.cookies_file)
        self.proxy.setText(s.proxy)
        self._sync_cookie_ui()

    def _export_rules(self):
        try:
            rules = glossary.parse_rules(self.replace_rules.toPlainText())
        except glossary.GlossaryError as e:
            QMessageBox.warning(self, "导出替换词典", f"当前规则有误，请先修正：\n{e}")
            return
        if not rules:
            QMessageBox.information(self, "导出替换词典", "当前没有规则可导出。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出替换词典", "替换词典.txt",
                                              "文本文件 (*.txt)")
        if not path:
            return
        try:
            Path(path).write_text(glossary.format_rules(rules) + "\n", encoding="utf-8")
        except OSError as e:
            QMessageBox.warning(self, "导出失败", str(e))
            return
        QMessageBox.information(self, "导出替换词典",
                                f"已导出 {len(rules)} 条规则到：\n{path}")

    def _import_rules(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入替换词典", "",
                                              "文本文件 (*.txt);;所有文件 (*.*)")
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            QMessageBox.warning(self, "导入失败", str(e))
            return
        try:
            incoming = glossary.parse_rules(text)
        except glossary.GlossaryError as e:
            QMessageBox.warning(self, "导入替换词典", f"文件内容无法解析：\n{e}")
            return
        if not incoming:
            QMessageBox.information(self, "导入替换词典", "文件里没有规则。")
            return
        try:
            current = glossary.parse_rules(self.replace_rules.toPlainText())
        except glossary.GlossaryError:
            current = []      # 当前框里本身有错，直接以导入内容为准
        merged = glossary.merge_rules(current, incoming)
        self.replace_rules.setPlainText(glossary.format_rules(merged))
        QMessageBox.information(
            self, "导入替换词典",
            f"已导入 {len(incoming)} 条，当前共 {len(merged)} 条。\n点「保存」后生效。")

    def _on_save(self):
        s = self.settings
        s.asr_engine = self.asr_engine.currentData()
        s.whisper_model = self.whisper_model.currentData()
        s.always_asr = self.always_asr.isChecked()
        try:
            rules = glossary.parse_rules(self.replace_rules.toPlainText())
        except glossary.GlossaryError as e:
            QMessageBox.warning(self, "替换词典有误", str(e))
            return
        s.glossary_terms = glossary.parse_terms(self.glossary_terms.toPlainText())
        s.glossary_fuzzy = self.glossary_fuzzy.isChecked()
        s.replace_rules = [r.to_dict() for r in rules]
        s.llm_provider = self.llm_provider.currentData() or "ollama"
        s.ollama_base = self.ollama_base.text().strip() or s.ollama_base
        s.ollama_model = self.ollama_model.currentText().strip() or s.ollama_model
        s.local_model_picked = True
        s.llamacpp_exe = self.llamacpp_exe.text().strip()
        s.llamacpp_model = self.llamacpp_model.text().strip()
        s.llamacpp_port = self.llamacpp_port.value()
        s.llamacpp_ngl = self.llamacpp_ngl.value()
        s.llamacpp_ctx = self.llamacpp_ctx.value()
        s.llamacpp_threads = self.llamacpp_threads.value()
        s.llamacpp_kv_q8 = self.llamacpp_kv_q8.isChecked()
        s.api_base = self.api_base.text().strip()
        s.api_key = self.api_key.text().strip()
        s.api_model = self.api_model.text().strip()
        s.fine_base = self.fine_base.text().strip()
        s.fine_key = self.fine_key.text().strip()
        s.fine_model = self.fine_model.text().strip()
        s.summary_granularity = self.summary_gran.currentData()
        s.export_enabled = self.export_enabled.isChecked()
        s.export_raw = self.export_raw.isChecked()
        s.export_timestamps = self.export_ts.isChecked()
        s.export_summary = self.export_summary.isChecked()
        s.export_dir = self.export_dir.text().strip()
        s.cookies_source = self.cookies_source.currentData()
        s.cookies_browser = self.cookies_browser.currentData()
        s.cookies_string = self.cookies_text.toPlainText().strip()
        s.cookies_file = self.cookies_file.text().strip()
        s.proxy = self.proxy.text().strip()
        err = self._validate(s)
        if err:
            QMessageBox.warning(self, "设置未保存", err)
            return
        save_settings(s)
        self.accept()

    @staticmethod
    def _validate(s: Settings) -> str:
        p = s.llm_provider
        if p == "ollama":
            if not s.ollama_base.strip():
                return "请填写 Ollama 地址，例如 http://127.0.0.1:11434/v1"
            if not s.ollama_model.strip():
                return "请选择或输入 Ollama 模型名；本机没有模型时先执行 ollama pull"
        elif p == "api":
            if not s.api_base.strip():
                return "请填写 API 地址，例如 https://api.deepseek.com/v1"
            if not s.api_model.strip():
                return "请填写 API 模型名，例如 deepseek-chat"
        elif p == "llamacpp":
            if s.llamacpp_exe.strip() and not Path(s.llamacpp_exe.strip()).exists():
                return "指定的 llama-server 可执行文件不存在"
            if s.llamacpp_model.strip() and not Path(s.llamacpp_model.strip()).exists():
                return "指定的 GGUF 模型文件不存在"
        if s.fine_base.strip() and not s.fine_model.strip():
            return "已填写精校 API 地址，还需要填写模型名"
        if s.cookies_source == "file" and not s.cookies_file.strip():
            return "Cookie 来源选了 cookies.txt 文件，但尚未选择文件"
        return ""

"""config 测试：导出目录解析、密钥 DPAPI 落盘、损坏配置备份（#10）。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bili import config as cfg


class ResolveExportDirTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self._patch = mock.patch.object(cfg, "app_dir", lambda: self.tmp)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_default_dir_under_app_dir(self):
        p = cfg.resolve_export_dir(cfg.Settings())
        self.assertEqual(p, self.tmp / "输出")
        self.assertTrue(p.is_dir())

    def test_relative_dir_under_app_dir(self):
        p = cfg.resolve_export_dir(cfg.Settings(export_dir="我的导出"))
        self.assertEqual(p, self.tmp / "我的导出")
        self.assertTrue(p.is_dir())

    def test_absolute_dir_is_honoured(self):
        target = self.tmp / "外部" / "深一层"
        p = cfg.resolve_export_dir(cfg.Settings(export_dir=str(target)))
        self.assertEqual(p, target)
        self.assertTrue(p.is_dir())


class PathsTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self._patch = mock.patch.object(cfg, "app_dir", lambda: self.tmp)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_data_paths_created(self):
        self.assertTrue(cfg.cache_dir().is_dir())
        self.assertTrue(cfg.models_dir().is_dir())
        self.assertEqual(cfg.log_file(), self.tmp / "data" / "logs" / "app.log")
        self.assertTrue(cfg.log_file().parent.is_dir())


class SecretStorageTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self._patch = mock.patch.object(cfg, "app_dir", lambda: self.tmp)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _config_json(self) -> dict:
        return json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))

    def test_roundtrip_preserves_secrets(self):
        s = cfg.Settings(api_key="sk-abc", fine_key="fk-def",
                         cookies_string="SESSDATA=xyz", ollama_model="qwen2.5:7b")
        cfg.save_settings(s)
        got = cfg.load_settings()
        self.assertEqual(got.api_key, "sk-abc")
        self.assertEqual(got.fine_key, "fk-def")
        self.assertEqual(got.cookies_string, "SESSDATA=xyz")
        self.assertEqual(got.ollama_model, "qwen2.5:7b")

    @unittest.skipUnless(cfg.secrets_encrypted(), "仅 Windows 有 DPAPI")
    def test_plaintext_never_written(self):
        cfg.save_settings(cfg.Settings(api_key="sk-abc", cookies_string="SESSDATA=xyz"))
        raw = (self.tmp / "config.json").read_text(encoding="utf-8")
        self.assertNotIn("sk-abc", raw)
        self.assertNotIn("SESSDATA=xyz", raw)
        data = self._config_json()
        self.assertTrue(data["api_key"].startswith(cfg._DPAPI_PREFIX))
        self.assertTrue(data["cookies_string"].startswith(cfg._DPAPI_PREFIX))

    @unittest.skipUnless(cfg.secrets_encrypted(), "仅 Windows 有 DPAPI")
    def test_legacy_plaintext_migrated_on_load(self):
        (self.tmp / "config.json").write_text(
            json.dumps({"api_key": "sk-old", "llm_provider": "api"}), encoding="utf-8")
        s = cfg.load_settings()
        self.assertEqual(s.api_key, "sk-old")
        self.assertEqual(s.llm_provider, "api")
        self.assertTrue(self._config_json()["api_key"].startswith(cfg._DPAPI_PREFIX))

    def test_corrupt_config_backed_up_not_silently_lost(self):
        (self.tmp / "config.json").write_text("{ 这不是 json", encoding="utf-8")
        s = cfg.load_settings()
        self.assertEqual(s.api_key, "")
        self.assertTrue((self.tmp / "config.corrupt.json").exists())

    def test_save_is_atomic_no_tmp_left_over(self):
        cfg.save_settings(cfg.Settings(api_key="k"))
        self.assertFalse((self.tmp / "config.tmp").exists())
        self.assertTrue((self.tmp / "config.json").exists())

    def test_unknown_fields_ignored(self):
        (self.tmp / "config.json").write_text(
            json.dumps({"不认识的字段": 1, "ollama_model": "qwen2.5:7b"}), encoding="utf-8")
        self.assertEqual(cfg.load_settings().ollama_model, "qwen2.5:7b")


class ConfigVersionTest(unittest.TestCase):
    """配置版本号与迁移：字段/默认值变更不能静默丢设置。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        self._patch = mock.patch.object(cfg, "app_dir", lambda: self.tmp)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _write(self, data: dict) -> None:
        (self.tmp / "config.json").write_text(json.dumps(data, ensure_ascii=False),
                                              encoding="utf-8")

    def _saved(self) -> dict:
        return json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))

    def test_current_version_written_on_save(self):
        cfg.save_settings(cfg.Settings())
        self.assertEqual(self._saved()["config_version"], cfg.CONFIG_VERSION)

    def test_roundtrip_keeps_version(self):
        cfg.save_settings(cfg.Settings(llm_provider="api"))
        s = cfg.load_settings()
        self.assertEqual(s.config_version, cfg.CONFIG_VERSION)
        self.assertEqual(s.llm_provider, "api")

    def test_v0_config_is_migrated_and_persisted(self):
        self._write({"ollama_model": "qwen3.8-9b-distill", "local_model_picked": True,
                     "export_dir": "我的导出"})
        s = cfg.load_settings()
        self.assertEqual(s.ollama_model, "qwen2.5:7b")
        self.assertFalse(s.local_model_picked)
        self.assertEqual(s.export_dir, "我的导出")     # 其它设置原样保留
        self.assertEqual(self._saved()["config_version"], cfg.CONFIG_VERSION)

    def test_future_version_backed_up_not_crash(self):
        self._write({"config_version": 99, "ollama_model": "新版本模型",
                     "未来的字段": "值"})
        s = cfg.load_settings()
        self.assertEqual(s.ollama_model, "新版本模型")
        self.assertTrue((self.tmp / "config.bak.json").exists())
        # 不静默回存：新版本配置仍保持原样，避免旧程序删字段
        self.assertEqual(self._saved()["config_version"], 99)

    def test_bad_version_value_treated_as_v0(self):
        self._write({"config_version": "abc", "ollama_model": "qwen3.8-9b-distill"})
        self.assertEqual(cfg.load_settings().ollama_model, "qwen2.5:7b")

    def test_migration_is_idempotent(self):
        self._write({"ollama_model": "qwen3.8-9b-distill"})
        cfg.load_settings()
        first = self._saved()
        cfg.load_settings()
        self.assertEqual(self._saved(), first)


class PortableSecretTest(unittest.TestCase):
    """非 Windows：优先系统钥匙串，其次本地密钥文件，最后才明文。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)
        patches = [mock.patch.object(cfg, "app_dir", lambda: self.tmp),
                   mock.patch.object(cfg, "_is_windows", lambda: False)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _fake_keyring(self):
        store: dict[tuple[str, str], str] = {}

        class FakeKeyring:
            @staticmethod
            def set_password(service, user, value):
                store[(service, user)] = value

            @staticmethod
            def get_password(service, user):
                return store.get((service, user))

        return FakeKeyring, store

    def test_keyring_preferred_when_available(self):
        fake, store = self._fake_keyring()
        with mock.patch.object(cfg, "_keyring_module", lambda: fake):
            stored = cfg._protect("api_key", "sk-abc")
            self.assertEqual(stored, "KEYRING:api_key")
            self.assertEqual(store[(cfg.APP_NAME, "api_key")], "sk-abc")
            self.assertEqual(cfg._unprotect("api_key", stored), "sk-abc")
            self.assertEqual(cfg.secret_storage_label(), "系统钥匙串")

    def test_local_fernet_fallback_encrypts(self):
        try:
            import cryptography.fernet  # noqa: F401
        except Exception:
            self.skipTest("未安装 cryptography")
        with mock.patch.object(cfg, "_keyring_module", lambda: None):
            stored = cfg._protect("api_key", "sk-abc")
            self.assertTrue(stored.startswith("LOCAL:"))
            self.assertNotIn("sk-abc", stored)
            self.assertEqual(cfg._unprotect("api_key", stored), "sk-abc")
            key_file = self.tmp / "data" / "secret.key"
            self.assertTrue(key_file.exists())
            self.assertGreaterEqual(len(key_file.read_bytes()), 32)

    def test_no_backend_falls_back_to_plaintext(self):
        with mock.patch.object(cfg, "_keyring_module", lambda: None), \
                mock.patch.object(cfg, "_local_protect", lambda plain: None):
            self.assertIsNone(cfg._protect("api_key", "sk-abc"))
            self.assertEqual(cfg.secret_storage_label(), "本地密钥文件")

    def test_save_and_load_roundtrip_without_windows(self):
        try:
            import cryptography.fernet  # noqa: F401
        except Exception:
            self.skipTest("未安装 cryptography")
        with mock.patch.object(cfg, "_keyring_module", lambda: None):
            cfg.save_settings(cfg.Settings(api_key="sk-abc", fine_key="fk-def"))
            raw = (self.tmp / "config.json").read_text(encoding="utf-8")
            self.assertNotIn("sk-abc", raw)
            self.assertNotIn("fk-def", raw)
            got = cfg.load_settings()
            self.assertEqual(got.api_key, "sk-abc")
            self.assertEqual(got.fine_key, "fk-def")

    def test_unknown_prefix_treated_as_unreadable(self):
        self.assertEqual(cfg._unprotect("api_key", "WEIRD:xxx"), None)


if __name__ == "__main__":
    unittest.main()

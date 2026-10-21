import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from acp_provider_config import provider_env_name, read_web_provider_config
from dsh_acp_client import sync_launch_overlay
from dsh_options_cache import options_cache_key
from first_run import RetrievalProbeThread


class AcpProviderConfigTests(unittest.TestCase):
    def test_retrieval_probe_thread_forwards_embedding_result(self):
        thread = RetrievalProbeThread(object())
        received = []
        thread.finished_with.connect(received.append)
        with patch("memory_embeddings.EmbeddingService.probe_api", return_value={
            "ok": True,
            "kind": "api",
            "identity": "api:bge-m3:http://localhost:11434/v1:1024",
            "message": "API 可用：bge-m3（1024 维）",
        }):
            thread.run()
        self.assertEqual(received[0]["ok"], True)
        self.assertIn("bge-m3", received[0]["message"])

    def test_web_patch_import_reads_provider_without_exposing_key(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cordis.patch.yml"
            path.write_text(
                """- id: llm-pi-ai\n  config:\n    providers:\n      demo:\n        apiKeyEnv: DEMO_TEST_KEY\n        baseURL: https://example.test/v1\n        models:\n          - id: model-a\n            name: Model A\n""",
                encoding="utf-8",
            )
            old = os.environ.get("DEMO_TEST_KEY")
            os.environ["DEMO_TEST_KEY"] = "secret-value"
            try:
                providers = read_web_provider_config(path)
            finally:
                if old is None:
                    os.environ.pop("DEMO_TEST_KEY", None)
                else:
                    os.environ["DEMO_TEST_KEY"] = old
            self.assertEqual(providers[0]["name"], "demo")
            self.assertEqual(providers[0]["base_url"], "https://example.test/v1")
            self.assertEqual(providers[0]["models"], ["model-a"])
            self.assertEqual(providers[0]["api_key"], "secret-value")

    def test_overlay_contains_route_not_secret(self):
        provider = {
            "name": "demo",
            "base_url": "https://example.test/v1",
            "api_key": "secret-value",
            "model": "model-a",
            "models": ["model-a"],
        }
        with tempfile.TemporaryDirectory() as temp:
            content = Path(sync_launch_overlay(None, root=temp, provider_info=provider)).read_text()
        self.assertIn("provider: demo", content)
        self.assertIn("baseURL: https://example.test/v1", content)
        self.assertIn("apiKeyEnv: AISSISTANT_ACP_DEMO_API_KEY", content)
        self.assertNotIn("secret-value", content)

    def test_dsh_catalog_supplies_models_and_reasoning(self):
        from dsh_model_catalog import catalog_dir, provider_catalog_models, provider_reasoning_map

        if catalog_dir() is None:
            self.skipTest("本机没有安装 DSH，跳过内置目录测试")
        models = provider_catalog_models("deepseek")
        self.assertTrue(models, "DSH 内置目录应包含 deepseek 模型")
        reasoning = provider_reasoning_map("deepseek")
        self.assertTrue(reasoning, "DeepSeek 官方模型应声明推理档位")
        declared = next(iter(reasoning.values()))
        self.assertTrue(set(declared) <= {"minimal", "low", "medium", "high", "xhigh", "max"})

    def test_official_overlay_keeps_dsh_catalog(self):
        provider = {
            "name": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "api": "openai-completions",
            "provider_kind": "official",
            "model": "deepseek-reasoner",
            "models": ["deepseek-reasoner", "deepseek-chat"],
        }
        with tempfile.TemporaryDirectory() as temp:
            content = Path(sync_launch_overlay(None, root=temp, provider_info=provider)).read_text()
        self.assertIn("provider: deepseek", content)
        self.assertIn("baseURL: https://api.deepseek.com/v1", content)
        self.assertNotIn("models:", content)

        self.assertEqual(provider_env_name("opencode-ds"), "AISSISTANT_ACP_OPENCODE_DS_API_KEY")
        self.assertNotEqual(options_cache_key("demo", "m", "https://one"), options_cache_key("demo", "m", "https://two"))
    def test_imported_api_key_env_survives_local_persistence(self):
        from acp_provider_config import load_acp_providers, save_acp_providers

        class Settings:
            def __init__(self):
                self.values = {}

            def value(self, key, default=""):
                return self.values.get(key, default)

            def setValue(self, key, value):
                self.values[key] = value

        settings = Settings()
        imported = {
            "name": "demo",
            "base_url": "https://example.test/v1",
            "api_key": "",
            "api_key_env": "DEMO_TEST_KEY",
            "models": ["model-a"],
        }
        save_acp_providers(settings, [imported])
        self.assertEqual(load_acp_providers(settings)[0]["api_key_env"], "DEMO_TEST_KEY")

    def test_empty_provider_list_stays_empty(self):
        from acp_provider_config import load_acp_providers, save_acp_providers

        class Settings:
            def __init__(self):
                self.values = {}

            def value(self, key, default=""):
                return self.values.get(key, default)

            def setValue(self, key, value):
                self.values[key] = value

        settings = Settings()
        save_acp_providers(settings, [])
        self.assertEqual(load_acp_providers(settings), [])


if __name__ == "__main__":
    unittest.main()

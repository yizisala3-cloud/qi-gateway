"""TASK-01：配置页密钥元数据与按项查看 API（§5.3 / §8.3 / L26–L28）。

* 元数据接口只返回非敏感字段（用途 / 来源 / 上游 / key_ref / 标签），
  绝无真实值；响应 no-store。
* reveal 接口按项返回真实值：鉴权 fail-closed、未配置真实报错、白名单
  外 404；响应 no-store。
* 反刍回退：来源与标识按实际生效值（CONTINUITY 回退）展示，与日志事件
  归属一致（TASK-02）。
* 标识秘密缺失：key_ref 为空且 key_ref_ready=false，不伪造可用状态。
"""

import unittest
from unittest import mock

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from gateway import credential_api
from gateway.logging_events import GatewayLogMiddleware, compute_key_ref

GATEWAY_TOKEN = "gateway-token-credential-test"
KEY_REF_SECRET = "k" * 40


class CredentialApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Starlette(
            routes=list(credential_api.credential_api_routes),
            middleware=[Middleware(GatewayLogMiddleware)],
        )

    def setUp(self):
        self.auth = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}
        base = {
            "GATEWAY_TOKEN": GATEWAY_TOKEN,
            "GATEWAY_KEY_REF_SECRET": KEY_REF_SECRET,
            "UPSTREAM_API_KEY": "sk-upstream-real-value-123456",
            "UPSTREAM_BASE_URL": "https://api.deepseek.com/v1",
            "ANALYSIS_API_KEY": "sk-analysis-real-value-123456",
            "ANALYSIS_BASE_URL": "https://api.siliconflow.cn/v1",
            "CONTINUITY_API_KEY": "sk-continuity-real-value-12345",
            "CONTINUITY_BASE_URL": "https://cont.example.com/v1",
            "RUMINATION_API_KEY": "",
            "RUMINATION_BASE_URL": "",
            "MEMORY_PLUGIN_TOKEN": "plugin-token-1234567890",
            "MCP_MEMORY_TOKEN": "",
            "TODO_PLUGIN_TOKEN": "",
        }
        for name, value in base.items():
            patch = mock.patch.object(credential_api.cfg, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.http = TestClient(self.app, raise_server_exceptions=False)

    # ── 鉴权（L28） ─────────────────────────────────────────────

    def test_token_is_required_for_metadata_and_reveal(self):
        for headers in ({}, {"Authorization": ""}, {"Authorization": "Bearer wrong"}):
            with self.subTest(headers=headers):
                response = self.http.get(
                    "/admin/api/config/credentials", headers=headers)
                self.assertEqual(response.status_code, 401)
                response = self.http.post(
                    "/admin/api/config/credentials/upstream/reveal", headers=headers)
                self.assertEqual(response.status_code, 401)

    def test_unconfigured_token_refuses_access(self):
        with mock.patch.object(credential_api.cfg, "GATEWAY_TOKEN", ""):
            response = self.http.get(
                "/admin/api/config/credentials", headers=self.auth)
            self.assertEqual(response.status_code, 401)

    # ── 元数据（L26） ───────────────────────────────────────────

    def test_metadata_never_contains_real_values(self):
        response = self.http.get(
            "/admin/api/config/credentials", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        body = response.text
        for secret in (
            "sk-upstream-real-value-123456",
            "sk-analysis-real-value-123456",
            "sk-continuity-real-value-12345",
            "plugin-token-1234567890",
        ):
            self.assertNotIn(secret, body)
        data = response.json()
        self.assertTrue(data["key_ref_ready"])
        by_id = {c["credential_id"]: c for c in data["credentials"]}
        # 白名单固定：模型 Key + 插件 / MCP / 待办 Token
        self.assertEqual(
            set(by_id),
            {"upstream", "analysis", "continuity", "rumination",
             "memory_plugin", "mcp_memory", "todo_plugin"})
        upstream = by_id["upstream"]
        self.assertTrue(upstream["configured"])
        self.assertEqual(upstream["source"], "UPSTREAM_API_KEY")
        self.assertEqual(upstream["provider"], "api.deepseek.com")
        self.assertEqual(upstream["key_ref"], compute_key_ref("sk-upstream-real-value-123456"))
        self.assertTrue(upstream["label"].startswith("K-"))
        # 未配置项：明确状态，无假星号值
        self.assertFalse(by_id["mcp_memory"]["configured"])
        self.assertEqual(by_id["mcp_memory"]["key_ref"], "")

    def test_rumination_fallback_reports_effective_source(self):
        # RUMINATION_API_KEY 为空 → 回退 CONTINUITY_API_KEY；标识与
        # continuity 同一身份（同一把密钥跨功能不变身份）
        response = self.http.get(
            "/admin/api/config/credentials", headers=self.auth)
        by_id = {c["credential_id"]: c for c in response.json()["credentials"]}
        rumination = by_id["rumination"]
        continuity = by_id["continuity"]
        self.assertTrue(rumination["configured"])
        self.assertTrue(rumination["fallback"])
        self.assertEqual(rumination["source"], "CONTINUITY_API_KEY")
        self.assertEqual(rumination["key_ref"], continuity["key_ref"])

    def test_key_ref_not_ready_is_reported_honestly(self):
        with mock.patch.object(credential_api.cfg, "GATEWAY_KEY_REF_SECRET", ""):
            response = self.http.get(
                "/admin/api/config/credentials", headers=self.auth)
        data = response.json()
        self.assertFalse(data["key_ref_ready"])
        for credential in data["credentials"]:
            self.assertEqual(credential["key_ref"], "")
            self.assertEqual(credential["label"], "")

    # ── 按项查看（L27 / L28） ───────────────────────────────────

    def test_reveal_returns_single_real_value_with_no_store(self):
        response = self.http.post(
            "/admin/api/config/credentials/upstream/reveal", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        data = response.json()
        self.assertEqual(data["credential_id"], "upstream")
        self.assertEqual(data["value"], "sk-upstream-real-value-123456")
        self.assertEqual(data["key_ref"], compute_key_ref("sk-upstream-real-value-123456"))
        self.assertEqual(data["source"], "UPSTREAM_API_KEY")
        # 响应只包含这一项的真实值
        self.assertNotIn("sk-analysis-real-value-123456", response.text)

    def test_reveal_unknown_credential_returns_404(self):
        response = self.http.post(
            "/admin/api/config/credentials/not-a-credential/reveal",
            headers=self.auth)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error_code"], "unknown_credential")

    def test_reveal_unconfigured_credential_returns_409(self):
        response = self.http.post(
            "/admin/api/config/credentials/mcp_memory/reveal", headers=self.auth)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error_code"], "credential_not_configured")
        self.assertNotIn("value", response.json())

    def test_reveal_cannot_target_server_side_secrets(self):
        # 白名单外的服务端秘密（Supabase / 标识秘密 / GATEWAY_TOKEN）不可
        # 通过任意名字读取
        for credential_id in (
            "supabase_secret_key", "gateway_token", "gateway_key_ref_secret",
            "SUPABASE_SECRET_KEY", "env", "..%2f",
        ):
            with self.subTest(credential_id=credential_id):
                response = self.http.post(
                    f"/admin/api/config/credentials/{credential_id}/reveal",
                    headers=self.auth)
                self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import patch

import httpx

from discord_lucas_mcp import server


class DiscordTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_post_edit_preserve_id_and_payload(self):
        cases = (
            ("POST", "/guilds/101/channels", {"name": "研究"}),
            ("POST", "/channels/102/messages", {"content": "テスト"}),
            ("PATCH", "/guilds/101/roles/123", {"permissions": "16"}),
        )
        original_client = httpx.AsyncClient
        for method, path, payload in cases:
            requests = []

            def respond(request):
                requests.append(request)
                self.assertEqual(request.method, method)
                self.assertEqual(str(request.url), server.API_BASE + path)
                self.assertEqual(request.headers["Authorization"], "Bot mock-credential")
                import json
                self.assertEqual(json.loads(request.content), payload)
                return httpx.Response(200, json={"id": "101", "token": "secret"})

            client = original_client(transport=httpx.MockTransport(respond))
            with patch.object(server, "token", return_value="mock-credential"), patch.object(server.httpx, "AsyncClient", return_value=client):
                result = await server.discord_write(method, path, payload)
            self.assertEqual(len(requests), 1)
            self.assertEqual(result["data"]["id"], "101")
            self.assertEqual(result["data"]["token"], "[非表示]")

    async def test_ambiguous_write_failure_is_not_replayed(self):
        requests = []

        def timeout(request):
            requests.append(request)
            raise httpx.ReadTimeout("模擬タイムアウト", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
        with patch.object(server, "token", return_value="mock-credential"), patch.object(server.httpx, "AsyncClient", return_value=client):
            with self.assertRaisesRegex(ValueError, "読み取り確認"):
                await server.discord_write("POST", "/channels/1/messages", {"content": "一度だけ"})
        self.assertEqual(len(requests), 1)

    async def test_deletion_ban_and_prune_precede_credentials_and_network(self):
        with patch.object(server, "token", side_effect=AssertionError("認証情報へ到達")):
            for method, path in (("DELETE", "/channels/1"), ("PUT", "/guilds/1/bans/2"), ("POST", "/guilds/1/bulk-ban"), ("POST", "/guilds/1/prune")):
                with self.assertRaisesRegex(ValueError, "削除"):
                    await server.discord_write(method, path)

    async def test_multipart_and_query_support_non_message_resources(self):
        import tempfile
        from pathlib import Path

        def respond(request):
            self.assertEqual(request.url.params["wait"], "true")
            self.assertIn(b'name="file"', request.content)
            self.assertIn(b'name="name"', request.content)
            self.assertIn(b"sample-file", request.content)
            self.assertNotIn(b"payload_json", request.content)
            return httpx.Response(200, json={"id": "123"})

        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            file = Path(folder) / "test.png"
            file.write_bytes(b"sample-file")
            client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            with patch.object(server, "token", return_value="mock-credential"), patch.object(server.httpx, "AsyncClient", return_value=client):
                result = await server.discord_write(
                    "POST", "/guilds/1/stickers", query={"wait": "true"},
                    files=[str(file)], file_fields=["file"], form={"name": "test", "tags": "smile"},
                )
            self.assertEqual(result["data"]["id"], "123")

    def test_url_escape_is_rejected(self):
        for path in ("https://example.com", "//example.com", "/../users", "/%2e%2e/users", "/users?token=bad", "/users%5c.."):
            with self.assertRaises(ValueError):
                server.validate_path(path)


if __name__ == "__main__":
    unittest.main()

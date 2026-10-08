import unittest
from unittest.mock import patch

import httpx

from discord_lucas_mcp import server


class NoDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_resource_deletion_is_rejected_at_transport_boundary(self):
        cases = (
            ("DELETE", "/channels/1/messages/2"),
            ("DELETE", "/channels/1"),
            ("DELETE", "/guilds/1/roles/2"),
            ("DELETE", "/webhooks/1"),
            ("POST", "/channels/1/messages/bulk-delete"),
            ("POST", "/channels/1/messages/bulk%2ddelete"),
            ("POST", "/guilds/1/prune"),
        )
        for method, path in cases:
            with self.subTest(method=method, path=path):
                client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(204)))
                with patch.object(server, "token", return_value="mock-credential"), patch.object(server.httpx, "AsyncClient", return_value=client):
                    with self.assertRaisesRegex(ValueError, "削除"):
                        await server.request(method, path, body={})
                await client.aclose()

    async def test_removing_existing_attachments_is_rejected(self):
        writes = []

        def respond(request):
            if request.method == "GET":
                return httpx.Response(200, json={"attachments": [{"id": "777", "filename": "資料.pdf"}]})
            writes.append(request)
            return httpx.Response(200, json={"id": "2"})

        original_client = httpx.AsyncClient
        with patch.object(server, "token", return_value="mock-credential"), patch.object(server.httpx, "AsyncClient", side_effect=lambda **kwargs: original_client(transport=httpx.MockTransport(respond))):
            with self.assertRaisesRegex(ValueError, "添付.*削除"):
                await server.discord_write("PATCH", "/channels/1/messages/2", {"attachments": []})
        self.assertEqual(writes, [])

    async def test_pin_invite_webhook_role_and_archive_remain_available(self):
        cases = (
            ("PUT", "/channels/1/messages/pins/2", None),
            ("POST", "/channels/1/invites", {"max_age": 86400}),
            ("POST", "/channels/1/webhooks", {"name": "通知"}),
            ("POST", "/guilds/1/roles", {"name": "資料担当", "permissions": "16"}),
            ("PUT", "/guilds/1/members/2/roles/3", None),
            ("PATCH", "/channels/1", {"archived": True}),
            ("DELETE", "/channels/1/messages/pins/2", None),
            ("DELETE", "/guilds/1/members/2/roles/3", None),
        )
        original_client = httpx.AsyncClient
        for method, path, body in cases:
            calls = []

            def respond(request):
                calls.append(request)
                if request.method == "GET" and request.url.path.endswith("/roles"):
                    return httpx.Response(200, json=[{"id": "3", "permissions": "16"}])
                return httpx.Response(200, json={"id": "123"})

            with patch.object(server, "token", return_value="mock-credential"), patch.object(server.httpx, "AsyncClient", side_effect=lambda **kwargs: original_client(transport=httpx.MockTransport(respond))):
                result = await server.request(method, path, body=body)
            self.assertEqual(result["status"], 200)
            self.assertEqual(len(calls), 2 if method == "PUT" and "/members/" in path else 1)

    async def test_existing_administrator_role_cannot_be_assigned(self):
        original_client = httpx.AsyncClient
        writes = []

        def respond(request):
            if request.method == "GET":
                return httpx.Response(200, json=[{"id": "3", "permissions": "8"}])
            writes.append(request)
            return httpx.Response(204)

        with patch.object(server, "token", return_value="mock-credential"), patch.object(server.httpx, "AsyncClient", side_effect=lambda **kwargs: original_client(transport=httpx.MockTransport(respond))):
            for method, path, body in (
                ("PUT", "/guilds/1/members/2/roles/3", None),
                ("PATCH", "/guilds/1/members/2", {"roles": ["3"]}),
            ):
                with self.assertRaisesRegex(ValueError, "管理者"):
                    await server.request(method, path, body=body)
        self.assertEqual(writes, [])

    async def test_edit_without_attachment_replacement_remains_available(self):
        calls = []

        def respond(request):
            calls.append(request.method)
            return httpx.Response(200, json={"attachments": [{"id": "777"}]})

        original_client = httpx.AsyncClient
        with patch.object(server, "token", return_value="mock-credential"), patch.object(server.httpx, "AsyncClient", side_effect=lambda **kwargs: original_client(transport=httpx.MockTransport(respond))):
            await server.discord_write("PATCH", "/channels/1/messages/2", {"content": "追記"})
        self.assertEqual(calls, ["PATCH"])

    async def test_attachment_replacement_cannot_hide_in_multipart_or_race(self):
        with patch.object(server, "token", side_effect=AssertionError("認証情報への到達は禁止")):
            for body, form in (
                ({"attachments": [{"id": "777"}]}, None),
                (None, {"payload_json": '{"attachments": []}'}),
            ):
                with self.assertRaisesRegex(ValueError, "添付.*削除"):
                    await server.request("PATCH", "/channels/1/messages/2", body=body, form=form)

    async def test_unreviewed_mutation_and_administrator_grant_are_rejected(self):
        with patch.object(server, "token", side_effect=AssertionError("認証情報への到達は禁止")):
            for method, path, body in (
                ("POST", "/future/remove-all", {}),
                ("PATCH", "/guilds/1/roles/2", {"permissions": "8"}),
                ("PUT", "/guilds/1/bans/2", {}),
                ("POST", "/guilds/1/bulk-ban", {}),
            ):
                with self.assertRaises(ValueError):
                    await server.request(method, path, body=body)


if __name__ == "__main__":
    unittest.main()

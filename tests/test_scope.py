import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from discord_lucas_mcp import server
from discord_lucas_mcp.scope import enforce_scope


class ScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_inbound_dm_channel_is_permitted(self):
        metadata = AsyncMock(return_value={"type": 1})
        with patch.dict(os.environ, {"DISCORD_ALLOWED_GUILD_ID": "7", "DISCORD_ALLOWED_DM_CHANNEL_ID": "20"}):
            await enforce_scope("POST", "/channels/20/messages", {}, None, metadata)
            with self.assertRaises(ValueError):
                await enforce_scope("POST", "/channels/21/messages", {}, None, metadata)
            await enforce_scope("GET", "/channels/22/messages", None, None, metadata, "22")
            metadata.return_value = {"type": 3}
            with self.assertRaises(ValueError):
                await enforce_scope("POST", "/channels/20/messages", {}, None, metadata)

    async def test_channel_and_references_require_target_membership(self):
        metadata = AsyncMock(side_effect=lambda path: {"guild_id": "7" if path == "/channels/10" else "8"})
        with patch.dict(os.environ, {"DISCORD_ALLOWED_GUILD_ID": "7"}):
            await enforce_scope("POST", "/channels/10/messages", {"content": "ok"}, None, metadata)
            for path, body in (("/channels/11/messages", {}), ("/channels/10/messages", {"message_reference": {"channel_id": "11"}}), ("/guilds/8/channels", {}), ("/guilds/7/channels", {"parent_id": "11"})):
                with self.assertRaises(ValueError):
                    await enforce_scope("POST", path, body, None, metadata)

    async def test_private_dm_global_commands_and_other_webhooks_denied(self):
        metadata = AsyncMock(return_value={"guild_id": "8"})
        with patch.dict(os.environ, {"DISCORD_ALLOWED_GUILD_ID": "7"}):
            for method, path in (("POST", "/users/@me/channels"), ("GET", "/applications/1/commands"), ("GET", "/webhooks/2"), ("GET", "/guilds/8/channels")):
                with self.assertRaises(ValueError):
                    await enforce_scope(method, path, None, None, metadata)

    async def test_member_profile_and_target_webhook_allowed(self):
        metadata = AsyncMock(return_value={"guild_id": "7"})
        with patch.dict(os.environ, {"DISCORD_ALLOWED_GUILD_ID": "7"}):
            await enforce_scope("GET", "/users/20", None, None, metadata)
            metadata.assert_awaited_with("/guilds/7/members/20")
            await enforce_scope("PATCH", "/webhooks/2", {"name": "ok"}, None, metadata)

    async def test_guild_list_exposes_only_target(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=[{"id": "7"}, {"id": "8"}])))
        with patch.dict(os.environ, {"DISCORD_ALLOWED_GUILD_ID": "7"}), patch.object(server, "token", return_value="mock"), patch.object(server.httpx, "AsyncClient", return_value=client):
            result = await server.request("GET", "/users/@me/guilds")
        self.assertEqual(result["data"], [{"id": "7"}])

    async def test_upload_outside_attachment_root_denied_before_send(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder)
            permitted = root / "attachments"
            permitted.mkdir()
            private = root / "private.txt"
            private.write_text("canary", encoding="utf-8")
            with patch.dict(os.environ, {"DISCORD_ALLOWED_FILE_ROOT": str(permitted)}), patch.object(server, "token", return_value="mock"), patch.object(server.httpx, "AsyncClient", side_effect=AssertionError("network")):
                with self.assertRaisesRegex(ValueError, "専用フォルダ"):
                    await server.request("POST", "/channels/10/messages", files=[str(private)])


if __name__ == "__main__":
    unittest.main()

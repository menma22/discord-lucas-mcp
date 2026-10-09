import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_listener as baseline
from discord_lucas_mcp.listener import ERROR_REPLY, Store, qualifies


message = baseline.message


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = baseline.DispatchTests.asyncSetUp
    asyncTearDown = baseline.DispatchTests.asyncTearDown
    def bot_role(self):
        return SimpleNamespace(id=404, tags=SimpleNamespace(bot_id=202))

    def test_bot_managed_role_is_a_mention_but_other_roles_are_not(self):
        incoming = message(mentioned=False)
        incoming.role_mentions = [self.bot_role()]
        self.assertTrue(qualifies(incoming, self.settings))
        incoming.role_mentions[0].tags.bot_id = 999
        self.assertFalse(qualifies(incoming, self.settings))
        incoming.role_mentions = [SimpleNamespace(id=404, tags=None)]
        self.assertFalse(qualifies(incoming, self.settings))
        incoming.role_mentions = [self.bot_role()]
        incoming.guild.id = 999
        self.assertFalse(qualifies(incoming, self.settings))

    async def test_edit_accepts_an_unhandled_mention_once_without_repeating_completed_work(self):
        channel = SimpleNamespace(id=3, guild=SimpleNamespace(id=101), fetch_message=AsyncMock(return_value=message()))
        payload = SimpleNamespace(guild_id=101, channel_id=3, message_id=1, data={"content": "<@202> 質問"})
        runs = []
        self.listener.runner = lambda settings, prompt: runs.append(json.loads(prompt)) or "編集への回答"
        with patch.object(self.listener, "get_channel", return_value=channel):
            await self.listener.on_raw_message_edit(payload)
            await self.listener.queues[3].join()
            await self.listener.on_raw_message_edit(payload)
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(self.posts), 1)
        channel.fetch_message.assert_awaited_once_with(1)

    async def test_other_guild_and_embed_only_edit_do_not_fetch_or_start_codex(self):
        with patch.object(self.listener, "fetch_channel", new=AsyncMock()) as fetch:
            await self.listener.on_raw_message_edit(SimpleNamespace(guild_id=999, channel_id=3, message_id=1, data={"content": "<@202>"}))
            await self.listener.on_raw_message_edit(SimpleNamespace(guild_id=101, channel_id=3, message_id=1, data={"embeds": []}))
            fetch.assert_not_awaited()
        self.assertEqual(self.posts, [])

    async def test_empty_gateway_content_is_refetched_for_an_authorized_role_mention(self):
        incoming = message(mentioned=False, content="")
        incoming.role_mentions = [self.bot_role()]
        fetched = message(mentioned=False, content="<@&404> ロールへの質問")
        fetched.role_mentions = [self.bot_role()]
        incoming.channel.fetch_message = AsyncMock(return_value=fetched)
        received = []
        self.listener.runner = lambda settings, prompt: received.append(json.loads(prompt)["incoming"]["content"]) or "回答"
        await self.listener.on_message(incoming)
        await self.listener.queues[3].join()
        self.assertEqual(received, [fetched.content])

    async def test_rest_recovers_a_missed_mention_and_role_once_and_ignores_ordinary_posts(self):
        role = self.bot_role()
        guild = SimpleNamespace(id=101, text_channels=[SimpleNamespace(id=3)], threads=[], get_role=lambda identifier: role if identifier == 404 else None)
        self.listener._connection._guilds[101] = guild
        self.store.recovery_since = "2020-01-01T00:00:00+00:00"
        data = [
            {"id": "4", "content": "普通の会話", "author": {"id": "55"}, "timestamp": "2026-10-09T10:00:00+00:00", "mentions": []},
            {"id": "3", "content": "<@&404> 質問", "author": {"id": "55"}, "timestamp": "2026-10-09T10:00:00+00:00", "mentions": [], "mention_roles": ["404"]},
            {"id": "2", "content": "<@202> 質問", "author": {"id": "55"}, "timestamp": "2026-10-09T10:00:00+00:00", "mentions": [{"id": "202"}]},
        ]
        posted, runs = [], []
        async def request(method, path, body=None, **kwargs):
            if method == "GET":
                return {"data": data}
            posted.append(body)
            return {"data": {"id": "888"}}
        self.listener.api_request = request
        self.listener.runner = lambda settings, prompt: runs.append(json.loads(prompt)["incoming"]["message_id"]) or "回復回答"
        await self.listener.recover_inputs()
        await asyncio.gather(*(queue.join() for queue in self.listener.queues.values()))
        await self.listener.recover_inputs()
        self.assertEqual(runs, ["2", "3"])
        self.assertEqual(len(posted), 2)
        self.assertIsNone(self.store.connection.execute("SELECT state FROM messages WHERE message_id='4'").fetchone())

    async def test_old_messages_before_recovery_floor_are_not_replayed(self):
        guild = SimpleNamespace(id=101, text_channels=[SimpleNamespace(id=3)], threads=[], get_role=lambda identifier: None)
        self.listener._connection._guilds[101] = guild
        self.store.recovery_since = "2026-10-09T10:00:00+00:00"
        self.listener.api_request = AsyncMock(return_value={"data": [{"id": "2", "content": "<@202>", "author": {"id": "55"}, "timestamp": "2020-01-01T00:00:00+00:00", "mentions": [{"id": "202"}]}]})
        await self.listener.recover_inputs()
        self.assertEqual(self.listener.queues, {})

    async def test_restart_recovers_accepted_input_without_rerunning_uncertain_processing(self):
        pending = {"message_id": "10", "channel_id": "3", "guild_id": "101", "content": "未実行"}
        uncertain = {**pending, "message_id": "11", "content": "実行したかもしれない"}
        self.store.claim("10", "3", pending)
        self.store.claim("11", "3", uncertain)
        self.store.state("11", "processing")
        self.store.connection.close()
        self.store = Store(Path(self.temporary.name) / "state.sqlite3", 10)
        self.listener.store = self.store
        runs = []
        self.listener.runner = lambda settings, prompt: runs.append(json.loads(prompt)["incoming"]["message_id"]) or "回答"
        await self.listener.recover_inbox()
        await asyncio.gather(*(queue.join() for queue in self.listener.queues.values()))
        await self.listener.recover_replies()
        self.assertEqual(runs, ["10"])
        self.assertEqual(self.store.reply("11")["response"], ERROR_REPLY)
        self.assertEqual(self.store.state_of("11"), "codex_failed")

    async def test_periodic_recovery_works_even_while_gateway_is_disconnected(self):
        recovered = asyncio.Event()
        async def recover():
            recovered.set()
        self.listener.recovery_interval = 0.01
        self.listener.inbox_recovered = True
        with patch.object(self.listener, "is_ready", return_value=False), patch.object(self.listener, "recover_inputs", side_effect=recover):
            await self.listener.setup_hook()
            await asyncio.wait_for(recovered.wait(), 1)
        self.assertFalse(json.loads((self.settings.runtime_dir / "listener-status.json").read_text(encoding="utf-8"))["connected"])


if __name__ == "__main__":
    unittest.main()

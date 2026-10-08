import os
import re
from typing import Any, Awaitable, Callable


async def enforce_scope(
    method: str, path: str, body: Any, query: dict[str, str] | None,
    read_metadata: Callable[[str], Awaitable[dict]],
    allowed_dm_channel_id: str | None = None,
) -> None:
    target = os.environ.get("DISCORD_ALLOWED_GUILD_ID")
    if not target:
        return

    async def channel(channel_id: str) -> None:
        if not channel_id.isdecimal():
            raise ValueError("対象チャンネルIDが不正。")
        metadata = await read_metadata(f"/channels/{channel_id}")
        dm_channel = allowed_dm_channel_id or os.environ.get("DISCORD_ALLOWED_DM_CHANNEL_ID")
        if dm_channel == channel_id and metadata.get("type") == 1:
            return
        if str(metadata.get("guild_id")) != target:
            raise ValueError("この自動応答から別サーバーのチャンネルは操作・閲覧できない。")

    guild = re.match(r"/guilds/(\d+)(/|$)", path)
    own_channel = re.match(r"/channels/(\d+)(/|$)", path)
    webhook = re.match(r"/webhooks/(\d+)(/|$)", path)
    command = re.match(r"/applications/\d+/guilds/(\d+)(/|$)", path)
    stage = re.fullmatch(r"/stage-instances/(\d+)", path)
    user = re.fullmatch(r"/users/(\d+)", path)
    if guild or command:
        if (guild or command)[1] != target:
            raise ValueError("この自動応答は対象サーバーの範囲に限定されている。")
    elif own_channel:
        await channel(own_channel[1])
    elif webhook:
        metadata = await read_metadata(f"/webhooks/{webhook[1]}")
        if str(metadata.get("guild_id")) != target:
            raise ValueError("別サーバーのWebhookは操作・閲覧できない。")
    elif method == "GET" and path in ("/users/@me", "/users/@me/guilds"):
        pass
    elif method == "GET" and user:
        await read_metadata(f"/guilds/{target}/members/{user[1]}")
    elif stage:
        metadata = await read_metadata(path)
        if str(metadata.get("guild_id")) != target:
            raise ValueError("別サーバーのステージは操作・閲覧できない。")
    elif method == "POST" and path == "/stage-instances" and isinstance(body, dict) and body.get("channel_id"):
        await channel(str(body["channel_id"]))
    else:
        raise ValueError("自動応答では個人DM・アプリ全体・未確認の範囲を操作できない。")

    async def references(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "guild_id" and item is not None and str(item) != target:
                    raise ValueError("他サーバーへの参照は使えない。")
                if key in ("channel_id", "parent_id") and item is not None:
                    await channel(str(item))
                await references(item)
        elif isinstance(value, list):
            for item in value:
                await references(item)

    await references(body)
    await references(query)

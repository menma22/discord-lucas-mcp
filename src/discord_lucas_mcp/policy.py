import re
from typing import Any
from urllib.parse import unquote


WRITE_ROUTES = {
    "POST": (
        r"/guilds",
        r"/guilds/\d+/(channels|roles|emojis|stickers|scheduled-events|templates|soundboard-sounds)",
        r"/guilds/\d+/auto-moderation/rules",
        r"/channels/\d+/(messages|threads|invites|webhooks|followers|typing)",
        r"/channels/\d+/messages/\d+/(threads|crosspost)",
        r"/channels/\d+/polls/\d+/expire",
        r"/webhooks/\d+/[^/]+(/(github|slack))?",
        r"/applications/\d+/(commands|guilds/\d+/commands)",
        r"/stage-instances",
        r"/users/@me/channels",
    ),
    "PATCH": (
        r"/guilds/\d+",
        r"/guilds/\d+/(channels|roles)",
        r"/guilds/\d+/members/(@me|\d+)",
        r"/guilds/\d+/(roles|emojis|stickers|scheduled-events|soundboard-sounds)/\d+",
        r"/guilds/\d+/auto-moderation/rules/\d+",
        r"/channels/\d+(/messages/\d+)?",
        r"/webhooks/\d+(/[^/]+(/messages/\d+)?)?",
        r"/applications/\d+(/(commands/\d+|guilds/\d+/commands/\d+))?",
        r"/stage-instances/\d+",
        r"/users/@me",
    ),
    "PUT": (
        r"/guilds/\d+/members/\d+/roles/\d+",
        r"/channels/\d+/permissions/\d+",
        r"/channels/\d+/(messages/pins|pins)/\d+",
        r"/channels/\d+/messages/\d+/reactions/[^/]+/@me",
        r"/channels/\d+/thread-members/(@me|\d+)",
        r"/guilds/\d+/templates/[^/]+",
    ),
    "DELETE": (
        r"/channels/\d+/(messages/pins|pins)/\d+",
        r"/channels/\d+/messages/\d+/reactions/[^/]+/@me",
        r"/guilds/\d+/members/\d+/roles/\d+",
        r"/channels/\d+/thread-members/(@me|\d+)",
    ),
}


def canonical_path(path: str) -> str:
    for _ in range(5):
        decoded = unquote(path)
        if decoded == path:
            return path
        path = decoded
    raise ValueError("多重エンコードされた経路は受け付けない。")


def check_mutation(method: str, path: str, body: Any) -> str:
    normalized = canonical_path(path)
    if method == "GET":
        return normalized
    if not any(re.fullmatch(route, normalized) for route in WRITE_ROUTES.get(method, ())):
        raise ValueError("この接続では本体の削除・BAN・キック、および未確認の変更APIを実行できない。")
    check_administrator_grant(body)
    return normalized


def check_administrator_grant(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("permissions", "allow") and item is not None:
                if int(item) & (1 << 3):
                    raise ValueError("この接続から管理者権限を付与することはできない。")
            check_administrator_grant(item)
    elif isinstance(value, list):
        for item in value:
            check_administrator_grant(item)


def is_message_edit(method: str, path: str) -> bool:
    return method == "PATCH" and bool(re.fullmatch(
        r"/(channels/\d+/messages/\d+|webhooks/\d+/[^/]+/messages/\d+)", path,
    ))


def assigned_roles(method: str, path: str, body: Any) -> tuple[str, list[str]] | None:
    single = re.fullmatch(r"/guilds/(\d+)/members/\d+/roles/(\d+)", path)
    if method == "PUT" and single:
        return single[1], [single[2]]
    member = re.fullmatch(r"/guilds/(\d+)/members/(@me|\d+)", path)
    if method == "PATCH" and member and isinstance(body, dict) and "roles" in body:
        if not isinstance(body["roles"], list):
            raise ValueError("rolesはロールIDの配列で指定する。")
        return member[1], [str(role) for role in body["roles"]]
    return None

"""``minebot login | logout | accounts | servers``: the bot's accounts and the server profiles."""

from __future__ import annotations

import argparse
import os
import sys
import time

from rich.cells import cell_len

from minebot.shell.accounts import CLIENT_ID_VAR, AccountError, AccountStore
from minebot.shell.profiles import WORLD_MODES, ProfileError, ServerBook, ServerProfile


def _announce(url: str, code: str, expires_in_s: int) -> None:
    print()
    print(f"  用机器人的微软账号登录（不是你自己的账号）：{url}")
    print()
    print(f"      {' '.join(code)}")
    print()
    print(f"  输入上面的代码；{expires_in_s // 60} 分钟内有效。等待登录…", flush=True)


def login(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="minebot login", description="Sign the bot in with its Microsoft account.")
    parser.add_argument("--client-id", default=os.environ.get(CLIENT_ID_VAR, ""),
                        help=f"Azure app client id approved for the Minecraft API (default: ${CLIENT_ID_VAR})")
    args = parser.parse_args(argv)
    try:
        account = AccountStore().login(args.client_id, announce=_announce)
    except AccountError as exc:
        print(f"minebot login: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        return 130
    print(f"✓ 已登录 {account.name}。启动时用 --bot {account.name}，或写进 servers.toml 的 account。")
    return 0


def logout(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="minebot logout", description="Forget a bot account's stored login.")
    parser.add_argument("name")
    args = parser.parse_args(argv)
    try:
        removed = AccountStore().remove(args.name)
    except AccountError as exc:
        print(f"minebot logout: {exc}", file=sys.stderr)
        return 2
    print(f"已删除 {args.name} 的登录" if removed else f"没有 {args.name} 的登录")
    return 0 if removed else 1


def accounts(argv: list[str]) -> int:
    argparse.ArgumentParser(prog="minebot accounts", description="List the bot's official accounts.").parse_args(argv)
    store = AccountStore()
    names = store.names()
    if not names:
        print("还没有正版账号；离线服直接用 --bot 名字，正版服先 minebot login")
        return 0
    for name in names:
        try:
            account = store.load(name)
        except AccountError as exc:
            print(f"  {name}  ✗ {exc}")
            continue
        left = (account.expires_at - time.time()) / 3600 if account else 0
        state = f"令牌还有 {left:.0f} 小时" if left > 0 else "令牌已过期，下次启动时自动刷新"
        print(f"  {name}  正版 · uuid {account.uuid[:8]}… · {state}")
    return 0


def servers(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="minebot servers", description="Manage server profiles (servers.toml).")
    commands = parser.add_subparsers(dest="command")
    add = commands.add_parser("add", help="add or replace a profile")
    add.add_argument("name")
    add.add_argument("address", help="HOST:PORT")
    add.add_argument("--account", default="", help="bot account name")
    add.add_argument("--world", choices=WORLD_MODES, default="protected")
    add.add_argument("--goal", default="")
    add.add_argument("--port", type=int, help="client IPC port")
    add.add_argument("--default", action="store_true", help="make it the default profile")
    remove = commands.add_parser("rm", help="remove a profile")
    remove.add_argument("name")
    default = commands.add_parser("default", help="choose the default profile")
    default.add_argument("name")
    args = parser.parse_args(argv)
    try:
        book = ServerBook()
        if args.command == "add":
            book.put(ServerProfile(args.name, args.address, account=args.account, world=args.world, goal=args.goal,
                                   port=args.port), make_default=args.default)
            book.save()
            print(f"✓ 已保存 {args.name}（{book.path}）")
        elif args.command == "rm":
            book.remove(args.name)
            book.save()
            print(f"已删除 {args.name}")
        elif args.command == "default":
            if args.name not in book.profiles:
                raise ProfileError(f"没有档案 {args.name!r}")
            book.default = args.name
            book.save()
            print(f"默认服务器：{args.name}")
        else:
            if not book.profiles:
                print(f"还没有服务器档案（{book.path}）；用 minebot servers add NAME HOST:PORT 添加")
            for profile in book.profiles.values():
                mark = "*" if profile.name == book.default else " "
                world = "可改动" if profile.disposable else "保护"
                account = profile.account or "（运行配置）"
                print(f" {mark} {profile.name:<12} {profile.address:<24} {account}{' ' * max(1, 18 - cell_len(account))}世界:{world}")
    except ProfileError as exc:
        print(f"minebot servers: {exc}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str]) -> int:
    command, rest = argv[0], argv[1:]
    return {"login": login, "logout": logout, "accounts": accounts, "servers": servers}[command](rest)


__all__ = ["main"]

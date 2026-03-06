#!/usr/bin/env python3
"""Feishu Permission CLI — manage document/file access control.

Usage:
    perm_ctl.py list <token> --type <doc_type>
    perm_ctl.py add <token> --type <doc_type> --user <open_id_or_name> --perm <view|edit|full_access>
    perm_ctl.py remove <token> --type <doc_type> --user <open_id> [--member-type openid]
    perm_ctl.py public-get <token> --type <doc_type>
    perm_ctl.py public-set <token> --type <doc_type> --link <setting>
    perm_ctl.py transfer <token> --type <doc_type> --user <open_id>
"""

import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(BASE))

from agent.platforms.feishu.api import FeishuAPI, ContactStore  # noqa: E402

_PERM_DISPLAY = {
    "view": "view",
    "edit": "edit",
    "full_access": "full_access",
}

_MEMBER_TYPE_MAP = {
    "openid": "openid",
    "userid": "userid",
    "email": "email",
    "opendepartmentid": "opendepartmentid",
}


def _load_config():
    import yaml
    with open(BASE / "config.yaml") as f:
        return yaml.safe_load(f)


def _resolve_user(user_str: str, contacts: ContactStore) -> str:
    """Resolve user string to open_id. Accepts raw open_id or contact name."""
    if user_str.startswith("ou_"):
        return user_str
    oid = contacts.lookup(user_str)
    if oid:
        return oid
    print(f"ERROR: Cannot resolve user '{user_str}'. Use open_id or add to contacts first.",
          file=sys.stderr)
    sys.exit(1)


# ── Commands ─────────────────────────────────────────────

def cmd_list(args, api):
    resp = api.get(
        f"/open-apis/drive/v1/permissions/{args.token}/members",
        params={"type": args.type})
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)

    members = resp.get("data", {}).get("members", [])
    if not members:
        print("No collaborators found.")
        return

    print(f"{'Member ID':<45} {'Type':<15} {'Perm'}")
    print("-" * 75)
    for m in members:
        mid = m.get("member_id", "?")
        mtype = m.get("member_type", "?")
        perm = m.get("perm", "?")
        print(f"{mid:<45} {mtype:<15} {perm}")


def cmd_add(args, api, contacts):
    open_id = _resolve_user(args.user, contacts)

    body = {
        "member_type": "openid",
        "member_id": open_id,
        "perm": args.perm,
    }
    resp = api.post(
        f"/open-apis/drive/v1/permissions/{args.token}/members",
        body,
        params={"type": args.type, "need_notification": "true"})
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)

    member = resp.get("data", {}).get("member", {})
    print(f"Added: {member.get('member_id', open_id)} with {args.perm} access")


def cmd_remove(args, api):
    member_type = args.member_type or "openid"
    resp = api.delete(
        f"/open-apis/drive/v1/permissions/{args.token}/members/{args.user}",
        params={"type": args.type, "member_type": member_type})
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    print(f"Removed: {args.user}")


def cmd_public_get(args, api):
    resp = api.get(
        f"/open-apis/drive/v1/permissions/{args.token}/public",
        params={"type": args.type})
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)

    pub = resp.get("data", {})
    print(f"External access: {pub.get('external_access_entity', '?')}")
    print(f"Security entity: {pub.get('security_entity', '?')}")
    print(f"Comment entity: {pub.get('comment_entity', '?')}")
    print(f"Share entity: {pub.get('share_entity', '?')}")
    print(f"Link share entity: {pub.get('link_share_entity', '?')}")
    print(f"Invite external: {pub.get('invite_external', '?')}")


def cmd_public_set(args, api):
    body = {
        "link_share_entity": args.link,
    }
    resp = api.patch(
        f"/open-apis/drive/v1/permissions/{args.token}/public",
        body,
        params={"type": args.type})
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    print(f"Link sharing set to: {args.link}")


def cmd_transfer(args, api, contacts):
    open_id = _resolve_user(args.user, contacts)

    body = {
        "owner": {
            "member_type": "openid",
            "member_id": open_id,
        },
    }
    resp = api.post(
        f"/open-apis/drive/v1/permissions/{args.token}/members/transfer_owner",
        body,
        params={"type": args.type})
    if resp.get("code") != 0:
        print(f"ERROR: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)
    print(f"Ownership transferred to: {open_id}")


# ── CLI ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Feishu Permission CLI")
    sub = parser.add_subparsers(dest="command")

    # list
    ls = sub.add_parser("list")
    ls.add_argument("token")
    ls.add_argument("--type", required=True, help="Doc type (docx, sheet, ...)")

    # add
    ad = sub.add_parser("add")
    ad.add_argument("token")
    ad.add_argument("--type", required=True)
    ad.add_argument("--user", required=True, help="Open ID or contact name")
    ad.add_argument("--perm", required=True, choices=["view", "edit", "full_access"])

    # remove
    rm = sub.add_parser("remove")
    rm.add_argument("token")
    rm.add_argument("--type", required=True)
    rm.add_argument("--user", required=True, help="Open ID to remove")
    rm.add_argument("--member-type", default="openid")

    # public-get
    pg = sub.add_parser("public-get")
    pg.add_argument("token")
    pg.add_argument("--type", required=True)

    # public-set
    ps = sub.add_parser("public-set")
    ps.add_argument("token")
    ps.add_argument("--type", required=True)
    ps.add_argument("--link", required=True,
                    choices=["tenant_readable", "tenant_editable",
                             "anyone_readable", "anyone_editable", "off"])

    # transfer
    tr = sub.add_parser("transfer")
    tr.add_argument("token")
    tr.add_argument("--type", required=True)
    tr.add_argument("--user", required=True, help="New owner open_id or name")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    cfg = _load_config()
    api = FeishuAPI.from_config()
    contacts = ContactStore(
        cfg.get("feishu", {}).get("contacts", {}).get("store_path")
    )

    dispatch_simple = {
        "list": lambda: cmd_list(args, api),
        "remove": lambda: cmd_remove(args, api),
        "public-get": lambda: cmd_public_get(args, api),
        "public-set": lambda: cmd_public_set(args, api),
    }
    dispatch_contacts = {
        "add": lambda: cmd_add(args, api, contacts),
        "transfer": lambda: cmd_transfer(args, api, contacts),
    }

    if args.command in dispatch_simple:
        dispatch_simple[args.command]()
    elif args.command in dispatch_contacts:
        dispatch_contacts[args.command]()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

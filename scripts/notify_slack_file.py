#!/usr/bin/env python3
import os, sys, subprocess, pathlib
from typing import Optional

# Keychain service names (same style as your webhook script)
KEYCHAIN_TOKEN_SERVICE = "tradebot-slack-bot"
KEYCHAIN_CHAN_SERVICE  = "tradebot-slack-channel"

def _kc_get(service: str) -> Optional[str]:
    try:
        out = subprocess.check_output(
            ["security", "find-generic-password", "-s", service, "-w"],
            stderr=subprocess.DEVNULL
        )
        return out.decode().strip()
    except Exception:
        return None

def _get_token() -> Optional[str]:
    return os.environ.get("SLACK_BOT_TOKEN") or _kc_get(KEYCHAIN_TOKEN_SERVICE)

def _get_channel() -> Optional[str]:
    return os.environ.get("SLACK_CHANNEL") or _kc_get(KEYCHAIN_CHAN_SERVICE)

def main():
    if len(sys.argv) < 2:
        print("usage: notify_slack_file.py <path> [title] [initial_comment]", file=sys.stderr); sys.exit(2)

    token   = _get_token()
    channel = _get_channel()
    if not token or not channel:
        print("Missing SLACK_BOT_TOKEN and/or SLACK_CHANNEL (env or Keychain).", file=sys.stderr); sys.exit(1)

    p = pathlib.Path(sys.argv[1])
    if not p.exists():
        print(f"file not found: {p}", file=sys.stderr); sys.exit(1)

    title   = sys.argv[2] if len(sys.argv) > 2 else p.name
    comment = sys.argv[3] if len(sys.argv) > 3 else None

    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    client = WebClient(token=token)

    try:
        # files_upload_v2 requires the bot in the channel & a channel **ID** (C…)
        resp = client.files_upload_v2(
            channel=channel,
            file=str(p),
            title=title,
            initial_comment=comment or ""
        )
        # Debug print (safe) so you can see the channel list once; remove if noisy
        files = resp.get("files") or []
        print({"ok": resp.get("ok"), "files_count": len(files), "channels": files[0].get("channels") if files else []})
    except SlackApiError as e:
        # Show Slack's JSON error then exit nonzero
        print(f"files_upload_v2 failed: {getattr(e, 'response', {}).data if hasattr(e, 'response') else e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()

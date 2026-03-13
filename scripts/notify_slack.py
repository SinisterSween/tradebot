#!/usr/bin/env python3
"""
Tradebot notification helper.
Tries Discord first, then Slack — whichever webhook is configured.
Configure via env var or macOS keychain:
  Discord: DISCORD_WEBHOOK_URL  or  keychain service "tradebot-discord"
  Slack:   SLACK_WEBHOOK_URL    or  keychain service "tradebot-slack"
"""
import os, sys, json, urllib.request, subprocess


def _keychain(service: str) -> str | None:
    try:
        out = subprocess.check_output(
            ["security", "find-generic-password", "-s", service, "-w"],
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip() or None
    except Exception:
        return None


def get_discord_webhook() -> str | None:
    return os.environ.get("DISCORD_WEBHOOK_URL") or _keychain("tradebot-discord")


def get_slack_webhook() -> str | None:
    return os.environ.get("SLACK_WEBHOOK_URL") or _keychain("tradebot-slack")


def notify(text: str) -> None:
    """Send text to Discord (preferred) or Slack. Fails silently if neither configured."""

    discord_url = get_discord_webhook()
    if discord_url:
        # Discord webhook payload uses "content" key
        payload = {"content": text}
        _post(discord_url, payload, label="discord")
        return

    slack_url = get_slack_webhook()
    if slack_url:
        payload = {"text": text}
        _post(slack_url, payload, label="slack")
        return

    print("notify: no webhook configured (Discord or Slack) — skipping", file=sys.stderr)


def _post(url: str, payload: dict, label: str) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (tradebot, 1.0)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        print(f"notify({label}): HTTP {e.code} {e.reason} — {e.read().decode()}", file=sys.stderr)
    except Exception as e:
        print(f"notify({label}): post failed: {e}", file=sys.stderr)


def main():
    text = " ".join(sys.argv[1:]) or "Tradebot notification"
    notify(text)


if __name__ == "__main__":
    main()

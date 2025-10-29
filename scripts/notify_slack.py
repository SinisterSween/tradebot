#!/usr/bin/env python3
import os, sys, json, urllib.request, subprocess

def get_webhook():
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if url:
        return url
    try:
        # pull from macOS keychain (service name: tradebot-slack)
        out = subprocess.check_output(
            ["security", "find-generic-password", "-s", "tradebot-slack", "-w"],
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except Exception:
        return None

def main():
    url = get_webhook()
    if not url:
        # no webhook configured; fail quietly so this never breaks your jobs
        print("notify_slack: no webhook configured; skipping", file=sys.stderr)
        return

    text = " ".join(sys.argv[1:]) or "Tradebot notification"
    payload = {"text": text}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except Exception as e:
        print(f"notify_slack: post failed: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()

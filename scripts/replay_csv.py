#!/usr/bin/env python3
import time
import argparse
from pathlib import Path

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--src", required=True, help="historical CSV to replay")
    p.add_argument("--dst", required=True, help="live CSV to write to")
    p.add_argument("--sleep", type=float, default=1.0, help="seconds between rows")
    p.add_argument("--truncate", action="store_true", help="truncate dst first")
    args = p.parse_args()

    src = Path(args.src)
    dst = Path(args.dst)

    lines = src.read_text().strip().splitlines()
    header = lines[0]
    rows = lines[1:]

    if args.truncate or not dst.exists():
        dst.write_text(header + "\n")

    for row in rows:
        with dst.open("a") as f:
            f.write(row + "\n")
        print(f"[REPLAY] {row}")
        time.sleep(args.sleep)

if __name__ == "__main__":
    main()

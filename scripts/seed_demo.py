#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import web_app  # noqa: E402


def main() -> int:
    payload = json.loads((ROOT / "samples/demo_mistake.json").read_text(encoding="utf-8"))
    with web_app.connect_db() as conn:
        candidates = web_app.duplicate_candidates(conn, payload)
        if candidates:
            print("Demo record already exists; nothing changed.")
            return 0
        card = web_app.save_card(conn, payload)
    print(f"Added fictional demo record: {card['subject']} / {card['topic']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

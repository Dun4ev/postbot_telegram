#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Фоновое дозаполнение AI-подписей для перенесенной базы.
"""

import asyncio
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402


SHARD_INDEX = int(os.getenv("POSTBOT_FILL_CAPTIONS_SHARD_INDEX", "0"))
SHARD_TOTAL = int(os.getenv("POSTBOT_FILL_CAPTIONS_SHARD_TOTAL", "1"))


def _remaining() -> tuple[int, int]:
    con = sqlite3.connect(bot.DB_PATH)
    try:
        queue_empty = con.execute(
            """
            SELECT COUNT(*)
            FROM queue
            WHERE COALESCE(ai_caption, '') = ''
              AND kind IN ('photo', 'video', 'album')
              AND (? = 1 OR (id % ?) = ?)
            """,
            (SHARD_TOTAL, SHARD_TOTAL, SHARD_INDEX),
        ).fetchone()[0]
        assets_empty = con.execute(
            """
            SELECT COUNT(*)
            FROM assets
            WHERE COALESCE(ai_caption, '') = ''
              AND kind IN ('photo', 'video', 'album')
              AND (? = 1 OR (id % ?) = ?)
            """,
            (SHARD_TOTAL, SHARD_TOTAL, SHARD_INDEX),
        ).fetchone()[0]
        return int(queue_empty), int(assets_empty)
    finally:
        con.close()


async def main() -> None:
    batch_size = bot.FILL_CAPTIONS_LIMIT
    iteration = 0
    while True:
        queue_empty, assets_empty = _remaining()
        if queue_empty == 0 and assets_empty == 0:
            print("done: all captions filled", flush=True)
            return

        iteration += 1
        stats = await bot.fill_missing_captions(
            batch_size,
            shard_index=SHARD_INDEX,
            shard_total=SHARD_TOTAL,
        )
        print(
            {
                "iteration": iteration,
                "queue_empty_before": queue_empty,
                "assets_empty_before": assets_empty,
                **stats,
            },
            flush=True,
        )

        if stats.get("rate_limited"):
            print(
                f"sleeping: rate limit until later, seconds={bot.FILL_CAPTIONS_RATE_LIMIT_SLEEP}",
                flush=True,
            )
            await asyncio.sleep(bot.FILL_CAPTIONS_RATE_LIMIT_SLEEP)
            continue

        if (
            stats["queue_filled"] == 0
            and stats["assets_filled"] == 0
            and stats["skipped"] == 0
            and stats["errors"] == 0
        ):
            print("stopped: no progress", flush=True)
            return


if __name__ == "__main__":
    print(
        f"started: {datetime.now().isoformat(timespec='seconds')} "
        f"shard={SHARD_INDEX}/{SHARD_TOTAL}",
        flush=True,
    )
    asyncio.run(main())

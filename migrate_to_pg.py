"""One-time migration: SQLite manager.db -> PostgreSQL (Neon).

Usage:
    python migrate_to_pg.py <path_to_old_manager.db>

Requires DATABASE_URL in environment (or .env file).
Uses ON CONFLICT DO NOTHING (auto target) so it never overwrites existing rows
and is safe to re-run after an interruption.
"""
import os
import sys
import sqlite3
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if not DATABASE_URL:
    sys.exit("DATABASE_URL not set. Add it to .env first.")

SRC = sys.argv[1] if len(sys.argv) > 1 else "manager.db"

TABLES = [
    "users", "groups", "filters", "bad_words", "pending_captchas",
    "config", "approved_users", "global_bans", "user_mutes", "user_bans",
    "infraction_history", "scheduled_messages", "user_stats", "logs",
]

def column_names(cur, table):
    cur.execute(f'SELECT column_name FROM information_schema.columns WHERE table_name=%s ORDER BY ordinal_position', (table,))
    return [r["column_name"] for r in cur.fetchall()]

def main():
    src = sqlite3.connect(SRC)
    src.row_factory = sqlite3.Row
    pg = psycopg2.connect(DATABASE_URL, connect_timeout=20)
    cur = pg.cursor(cursor_factory=RealDictCursor)

    total = 0
    for table in TABLES:
        try:
            rows = src.execute(f'SELECT * FROM "{table}"').fetchall()
        except sqlite3.OperationalError as e:
            print(f"[skip] {table}: {e}", flush=True)
            continue
        if not rows:
            print(f"[ok] {table}: 0 rows", flush=True)
            continue

        src_cur = src.execute(f'SELECT * FROM "{table}" LIMIT 1')
        src_cols = [d[0] for d in src_cur.description]
        pg_cols = [c for c in column_names(cur, table) if c in src_cols]

        # id column is BIGSERIAL on PG -> exclude from insert
        if "id" in pg_cols and table not in ("users", "groups", "global_bans", "config", "user_stats"):
            pg_cols = [c for c in pg_cols if c != "id"]

        cols = ", ".join(f'"{c}"' for c in pg_cols)
        placeholders = ", ".join(["%s"] * len(pg_cols))
        sql = f'INSERT INTO "{table}" ({cols}) VALUES ({placeholders}) ON CONFLICT DO NOTHING'

        count = 0
        CHUNK = 200
        for i in range(0, len(rows), CHUNK):
            chunk = rows[i:i + CHUNK]
            batch = [[row[c] for c in pg_cols] for row in chunk]
            try:
                cur.executemany(sql, batch)
                pg.commit()
                count += len(batch)
                print(f"  {table}: +{count} / {len(rows)}", flush=True)
            except Exception as e:
                pg.rollback()
                # Fall back to row-by-row so a single bad row isn't fatal
                for row in chunk:
                    vals = [row[c] for c in pg_cols]
                    try:
                        cur.execute(sql, vals)
                        count += 1
                    except Exception as e2:
                        print(f"  [err] {table} row {row['id'] if 'id' in row.keys() else row}: {e2}", flush=True)
                pg.commit()
        total += count
        print(f"[ok] {table}: wrote {count} rows", flush=True)

    cur.close()
    pg.close()
    src.close()
    print(f"\nDone. Total rows written: {total}", flush=True)

if __name__ == "__main__":
    main()

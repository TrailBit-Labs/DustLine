"""Build the known_entities.db SQLite database from source data.

Usage:
    python data/build_db.py                          # Build from known_entities.json only
    python data/build_db.py --verify                 # Build + verify all JSON addresses present
    python data/build_db.py --orbitaal FILE           # Also ingest ORBITAAL TSV
    python data/build_db.py --tagpacks FILE           # Also ingest OXT TagPack CSV
    python data/build_db.py --graphsense DIR          # Also ingest GraphSense TagPack YAMLs
    python data/build_db.py --mining-pools DIR        # Also ingest btccom mining pool JSONs
"""

from __future__ import annotations

import csv
import json
import sqlite3
import sys
from pathlib import Path

DATA_DIR = Path(__file__).parent
JSON_PATH = DATA_DIR / "known_entities.json"
DB_PATH = DATA_DIR / "known_entities.db"

# Map JSON category keys to normalized category names
CATEGORY_MAP = {
    "exchanges": "exchange",
    "mining_pools": "mining_pool",
    "services": "service",
    "notable": "notable",
}


def create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entities (
            address TEXT PRIMARY KEY,
            entity TEXT NOT NULL,
            category TEXT,
            source TEXT,
            confidence TEXT DEFAULT 'confirmed'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entity ON entities(entity)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON entities(category)")
    conn.commit()


def seed_from_json(conn: sqlite3.Connection, json_path: Path) -> int:
    """Load known_entities.json and insert all addresses. Returns count."""
    if not json_path.exists():
        print(f"Warning: {json_path} not found, skipping JSON seed")
        return 0

    with open(json_path, "r") as f:
        data = json.load(f)

    count = 0
    for cat_key, cat_entries in data.get("entities", {}).items():
        category = CATEGORY_MAP.get(cat_key, cat_key)
        for entity_data in cat_entries.values():
            name = entity_data.get("name", "Unknown")
            for addr in entity_data.get("known_addresses", []):
                if not addr:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO entities "
                    "(address, entity, category, source, confidence) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (addr, name, category, "manual", "confirmed"),
                )
                count += 1

    conn.commit()
    return count


def ingest_orbitaal(conn: sqlite3.Connection, tsv_path: Path) -> int:
    """Ingest ORBITAAL dataset (TSV: address, entity, category)."""
    if not tsv_path.exists():
        print(f"Error: {tsv_path} not found")
        return 0

    count = 0
    with open(tsv_path, "r", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        for row in reader:
            if len(row) < 2:
                continue
            address = row[0].strip()
            entity = row[1].strip()
            category = row[2].strip() if len(row) > 2 else ""
            if not address or not entity:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO entities "
                "(address, entity, category, source, confidence) "
                "VALUES (?, ?, ?, ?, ?)",
                (address, entity, category, "orbitaal", "confirmed"),
            )
            count += 1

    conn.commit()
    return count


def ingest_tagpacks(conn: sqlite3.Connection, csv_path: Path) -> int:
    """Ingest OXT TagPack dataset (CSV: address, label, source, category)."""
    if not csv_path.exists():
        print(f"Error: {csv_path} not found")
        return 0

    count = 0
    with open(csv_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            address = row.get("address", "").strip()
            entity = row.get("label", "").strip()
            category = row.get("category", "").strip()
            if not address or not entity:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO entities "
                "(address, entity, category, source, confidence) "
                "VALUES (?, ?, ?, ?, ?)",
                (address, entity, category, "oxt_tagpack", "probable"),
            )
            count += 1

    conn.commit()
    return count


def ingest_graphsense(conn: sqlite3.Connection, packs_dir: Path) -> int:
    """Ingest GraphSense TagPack YAML files (BTC addresses only).

    Each YAML file has header-level defaults (label, currency, confidence,
    category, abuse) and a `tags` list with per-tag overrides.
    """
    import yaml

    CONFIDENCE_MAP = {
        "service_data": "confirmed",
        "authority_data": "confirmed",
        "forensic": "probable",
        "web_crawl": "cluster",
        "untrusted_transaction": "cluster",
        "ledger_immanent": "confirmed",
    }

    ABUSE_TO_CATEGORY = {
        "ransomware": "abuse",
        "phishing": "abuse",
        "sextortion": "abuse",
        "scam": "abuse",
        "ponzi_scheme": "abuse",
        "pyramid_scheme": "abuse",
        "service_hack": "abuse",
        "terrorism": "sanctioned",
        "extremism": "sanctioned",
        "sanction": "sanctioned",
    }

    if not packs_dir.is_dir():
        print(f"Error: {packs_dir} is not a directory")
        return 0

    yaml_files = sorted(packs_dir.glob("*.yaml"))
    if not yaml_files:
        print(f"Warning: no .yaml files found in {packs_dir}")
        return 0

    count = 0
    skipped_non_btc = 0

    for yf in yaml_files:
        file_count = 0
        try:
            with open(yf, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception as exc:
            print(f"  Warning: failed to parse {yf.name}: {exc}")
            continue

        if not data or not isinstance(data, dict):
            continue

        # Header-level defaults
        header_label = data.get("label", "")
        header_currency = data.get("currency", "")
        header_confidence = data.get("confidence", "")
        header_category = data.get("category", "")
        header_abuse = data.get("abuse", "")

        tags = data.get("tags", [])
        if not tags:
            continue

        for tag in tags:
            if not isinstance(tag, dict):
                continue

            # Currency: per-tag overrides header
            currency = str(tag.get("currency", header_currency)).strip()
            if currency != "BTC":
                skipped_non_btc += 1
                continue

            address = str(tag.get("address", "")).strip().strip("'\"")
            if not address:
                continue

            # Label: per-tag overrides header
            label = str(tag.get("label", header_label)).strip()
            if not label:
                continue

            # Category: per-tag > header > derive from abuse
            category = tag.get("category", "") or header_category
            if not category:
                abuse = tag.get("abuse", "") or header_abuse
                category = ABUSE_TO_CATEGORY.get(str(abuse).lower(), "")

            # Confidence mapping
            raw_conf = str(tag.get("confidence", header_confidence)).strip()
            confidence = CONFIDENCE_MAP.get(raw_conf, "cluster")

            conn.execute(
                "INSERT OR IGNORE INTO entities "
                "(address, entity, category, source, confidence) "
                "VALUES (?, ?, ?, ?, ?)",
                (address, label, str(category), "graphsense", confidence),
            )
            file_count += 1

        count += file_count

    conn.commit()
    if skipped_non_btc:
        print(f"  Skipped {skipped_non_btc} non-BTC addresses")
    return count


def ingest_mining_pools(conn: sqlite3.Connection, pools_dir: Path) -> int:
    """Ingest btccom mining pool JSON files.

    Each JSON file has: {"id": int, "name": str, "addresses": [...], "tags": [...], "link": str}
    """
    if not pools_dir.is_dir():
        print(f"Error: {pools_dir} is not a directory")
        return 0

    json_files = sorted(pools_dir.glob("*.json"))
    if not json_files:
        print(f"Warning: no .json files found in {pools_dir}")
        return 0

    count = 0
    for jf in json_files:
        try:
            with open(jf, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            print(f"  Warning: failed to parse {jf.name}: {exc}")
            continue

        if not isinstance(data, dict):
            continue

        name = data.get("name", "").strip()
        addresses = data.get("addresses", [])

        if not name or not addresses:
            continue

        for addr in addresses:
            addr = str(addr).strip()
            if not addr:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO entities "
                "(address, entity, category, source, confidence) "
                "VALUES (?, ?, ?, ?, ?)",
                (addr, name, "mining_pool", "mining_pools", "confirmed"),
            )
            count += 1

    conn.commit()
    return count


def verify_json_addresses(conn: sqlite3.Connection, json_path: Path) -> bool:
    """Verify all addresses from known_entities.json exist in the DB."""
    if not json_path.exists():
        print(f"Warning: {json_path} not found, cannot verify")
        return False

    with open(json_path, "r") as f:
        data = json.load(f)

    missing = []
    mismatched = []
    total = 0

    for cat_key, cat_entries in data.get("entities", {}).items():
        for entity_data in cat_entries.values():
            name = entity_data.get("name", "Unknown")
            for addr in entity_data.get("known_addresses", []):
                if not addr:
                    continue
                total += 1
                cursor = conn.execute(
                    "SELECT entity FROM entities WHERE address = ?",
                    (addr,),
                )
                row = cursor.fetchone()
                if row is None:
                    missing.append(addr)
                elif row[0] != name:
                    mismatched.append((addr, name, row[0]))

    ok = True
    if missing:
        print(f"FAIL: {len(missing)} addresses missing from DB:")
        for addr in missing:
            print(f"  - {addr}")
        ok = False

    if mismatched:
        print(f"FAIL: {len(mismatched)} addresses have wrong entity:")
        for addr, expected, actual in mismatched:
            print(f"  - {addr}: expected '{expected}', got '{actual}'")
        ok = False

    if ok:
        print(f"OK: All {total} addresses from JSON verified in DB")

    return ok


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Build known_entities.db")
    parser.add_argument("--verify", action="store_true", help="Verify JSON addresses")
    parser.add_argument("--orbitaal", type=Path, help="ORBITAAL TSV path")
    parser.add_argument("--tagpacks", type=Path, help="OXT TagPack CSV path")
    parser.add_argument("--graphsense", type=Path, help="GraphSense TagPack YAML directory")
    parser.add_argument("--mining-pools", type=Path, help="btccom mining pool JSON directory")
    parser.add_argument("--output", type=Path, default=DB_PATH, help="Output DB path")
    args = parser.parse_args()

    db_path = args.output

    # Remove existing DB to rebuild from scratch
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    create_schema(conn)

    # Seed from JSON
    json_count = seed_from_json(conn, JSON_PATH)
    print(f"Seeded {json_count} addresses from known_entities.json")

    # Optional: ingest external datasets
    if args.orbitaal:
        orb_count = ingest_orbitaal(conn, args.orbitaal)
        print(f"Ingested {orb_count} addresses from ORBITAAL")

    if args.tagpacks:
        tag_count = ingest_tagpacks(conn, args.tagpacks)
        print(f"Ingested {tag_count} addresses from OXT TagPacks")

    if args.mining_pools:
        mp_count = ingest_mining_pools(conn, args.mining_pools)
        print(f"Ingested {mp_count} addresses from mining pools")

    if args.graphsense:
        gs_count = ingest_graphsense(conn, args.graphsense)
        print(f"Ingested {gs_count} BTC addresses from GraphSense TagPacks")

    # Total count
    cursor = conn.execute("SELECT COUNT(*) FROM entities")
    total = cursor.fetchone()[0]
    print(f"Total: {total} addresses in {db_path}")

    # Verify if requested
    if args.verify:
        print()
        if not verify_json_addresses(conn, JSON_PATH):
            conn.close()
            sys.exit(1)

    conn.close()


if __name__ == "__main__":
    main()

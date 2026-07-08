import csv
import os
from pathlib import Path
from typing import Optional

import psycopg
from psycopg.rows import dict_row
import requests

try:
    import streamlit as st
except Exception:
    st = None


HEADERS = {
    "User-Agent": "personal-mtg-inventory/1.0",
    "Accept": "application/json;q=0.9,*/*;q=0.8",
}


def get_database_url() -> str:
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        return db_url

    if st is not None:
        return st.secrets["database"]["url"]

    raise ValueError("DATABASE_URL not found in environment or Streamlit secrets.")


def get_connection():
    return psycopg.connect(
        get_database_url(),
        row_factory=dict_row,
    )


def normalize_key(value: str) -> str:
    return (value or "").strip().lower().replace(" ", "_").replace("-", "_")


def normalized_row(row: dict) -> dict:
    return {normalize_key(k): v for k, v in row.items()}


def row_value(row: dict, *keys: str) -> str:
    for key in keys:
        if key in row and row[key] is not None:
            return str(row[key]).strip()
    return ""

def combined_face_mana_cost(card: dict) -> Optional[str]:
    top_level = card.get("mana_cost")
    if top_level not in (None, ""):
        return top_level
    
    faces = card.get("card_faces") or []
    parts = []
    for face in faces:
        mana_cost = face.get("mana_cost")
        if mana_cost:
            parts.append(mana_cost)
    
    if not parts:
        return None
    
    return " // ".join(parts)

def combined_face_oracle_text(card: dict) -> Optional[str]:
    top_level = card.get("oracle_text")
    if top_level not in (None, ""):
        return top_level
    
    faces = card.get("card_faces") or []
    parts = []
    
    for face in faces:
        name = (face.get("name") or "").strip()
        text = (face.get("oracle_text") or "").strip()
        
        if name and text:
            parts.append(f"{name}\n{text}")
        elif text:
            parts.append(text)
    
    if not parts:
        return None
    
    return "\n\n//\n\n".join(parts)

def combined_face_color_identity(card: dict) -> Optional[str]:
    top_level = format_color_identity(card.get("color_identity", []))
    if top_level is not None:
        return top_level

    faces = card.get("card_faces") or []
    combined_colors = []

    for face in faces:
        for color in face.get("colors", []) or []:
            if color not in combined_colors:
                combined_colors.append(color)

    return format_color_identity(combined_colors)

def detect_import_type(fieldnames: list[str], requested_type: str) -> str:
    if requested_type in {"manual", "manabox"}:
        return requested_type

    headers = {normalize_key(name) for name in (fieldnames or [])}

    if "finish" in headers:
        return "manual"

    if "foil" in headers and "quantity" in headers and ("scryfall_id" in headers or "name" in headers):
        return "manabox"

    raise ValueError("Could not detect CSV type. Use a file with either a 'finish' column (manual) or a ManaBox export with a 'Foil' column.")


def parse_finish(value: Optional[str]) -> str:
    raw = (value or "").strip().lower()

    mapping = {
        "": "nonfoil",
        "normal": "nonfoil",
        "nonfoil": "nonfoil",
        "false": "nonfoil",
        "no": "nonfoil",
        "0": "nonfoil",
        "foil": "foil",
        "true": "foil",
        "yes": "foil",
        "1": "foil",
        "etched": "etched",
    }

    if raw not in mapping:
        raise ValueError(f"Unexpected finish/foil value: {value}")

    return mapping[raw]


def parse_quantity(value: Optional[str]) -> int:
    raw = (value or "").strip()
    if raw == "":
        raise ValueError("Quantity is blank")
    qty = int(raw)
    if qty < 0:
        raise ValueError("Quantity cannot be negative")
    return qty


def clean_price(value: Optional[str]) -> Optional[float]:
    raw = (value or "").strip() if isinstance(value, str) else value
    if raw in (None, ""):
        return None
    return float(raw)


def format_color_identity(colors: list[str]) -> str | None:
    if not colors:
        return None

    mapping = {
        "W": "White",
        "U": "Blue",
        "B": "Black",
        "R": "Red",
        "G": "Green",
    }

    names = [mapping[c] for c in colors if c in mapping]
    if not names:
        return None
    if len(names) == 5:
        return "All"
    return "/".join(names)


def fetch_card_by_id_from_scryfall(scryfall_id: str) -> dict:
    resp = requests.get(
        f"https://api.scryfall.com/cards/{scryfall_id}",
        headers=HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()

def fetch_card_by_set_and_number_from_scryfall(set_code: str, collector_number: str) -> dict:
    resp = requests.get(
        f"https://api.scryfall.com/cards/{set_code.lower()}/{collector_number}",
        headers=HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()

def ensure_card_printing_exists_by_set_and_number(conn, set_code: str, collector_number: str) -> str:
    scryfall_id = get_scryfall_id_by_set_and_number(conn, set_code, collector_number)
    if scryfall_id is not None:
        return scryfall_id
    
    card = fetch_card_by_set_and_number_from_scryfall(set_code, collector_number)
    upsert_card_printing(conn, card)
    return card["id"]

def create_temp_seen_table(conn) -> None:
    conn.execute(
        """
        CREATE TEMP TABLE temp_personal_seen (
            scryfall_id TEXT NOT NULL,
            finish TEXT NOT NULL,
            PRIMARY KEY (scryfall_id, finish)
        )
        """
    )

def mark_seen(conn, *, scryfall_id: str, finish: str) -> None:
    conn.execute(
        """
        INSERT INTO temp_personal_seen (scryfall_id, finish)
        VALUES (%s, %s)
        ON CONFLICT DO NOTHING
        """,
        (scryfall_id, finish),
    )

def zero_missing_inventory_rows(conn) -> int:
    cur = conn.execute(
        """
        UPDATE inventory
        SET
            stock = 0,
            updated_at = CURRENT_TIMESTAMP
        WHERE stock <> 0
        AND NOT EXISTS (
            SELECT 1
            FROM temp_personal_seen s
            WHERE s.scryfall_id = inventory.scryfall_id
                AND s.finish = inventory.finish
        )
        """
    )
    return cur.rowcount



def upsert_card_printing(conn, card: dict) -> None:
    prices = card.get("prices", {}) or {}
    legalities = card.get("legalities", {}) or {}

    conn.execute(
        """
        INSERT INTO card_printings (
            scryfall_id,
            oracle_id,
            card_name,
            collector_number,
            set_code,
            set_name,
            mana_cost,
            mana_value,
            mana_symbols,
            color_identity,
            rarity,
            type_line,
            oracle_text,
            special_notes,
            usd_price,
            usd_foil_price,
            usd_etched_price,
            rarity_floor_value,
            standard_legal,
            pioneer_legal,
            modern_legal,
            commander_legal,
            updated_at
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP
        )
        ON CONFLICT (scryfall_id) DO UPDATE SET
            oracle_id = EXCLUDED.oracle_id,
            card_name = EXCLUDED.card_name,
            collector_number = EXCLUDED.collector_number,
            set_code = EXCLUDED.set_code,
            set_name = EXCLUDED.set_name,
            mana_cost = EXCLUDED.mana_cost,
            mana_value = EXCLUDED.mana_value,
            mana_symbols = EXCLUDED.mana_symbols,
            color_identity = EXCLUDED.color_identity,
            rarity = EXCLUDED.rarity,
            type_line = EXCLUDED.type_line,
            oracle_text = EXCLUDED.oracle_text,
            special_notes = EXCLUDED.special_notes,
            usd_price = EXCLUDED.usd_price,
            usd_foil_price = EXCLUDED.usd_foil_price,
            usd_etched_price = EXCLUDED.usd_etched_price,
            rarity_floor_value = EXCLUDED.rarity_floor_value,
            standard_legal = EXCLUDED.standard_legal,
            pioneer_legal = EXCLUDED.pioneer_legal,
            modern_legal = EXCLUDED.modern_legal,
            commander_legal = EXCLUDED.commander_legal,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            card.get("id"),
            card.get("oracle_id"),
            card.get("name"),
            card.get("collector_number"),
            card.get("set"),
            card.get("set_name"),
            combined_face_mana_cost(card),
            card.get("cmc"),
            combined_face_mana_cost(card),
            combined_face_color_identity(card),
            card.get("rarity"),
            card.get("type_line"),
            combined_face_oracle_text(card),
            None,
            clean_price(prices.get("usd")),
            clean_price(prices.get("usd_foil")),
            clean_price(prices.get("usd_etched")),
            None,
            legalities.get("standard"),
            legalities.get("pioneer"),
            legalities.get("modern"),
            legalities.get("commander"),
        ),
    )


def ensure_card_printing_exists(conn, scryfall_id: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM card_printings WHERE scryfall_id = %s",
        (scryfall_id,),
    ).fetchone()

    if row is not None:
        return

    card = fetch_card_by_id_from_scryfall(scryfall_id)
    upsert_card_printing(conn, card)


def get_scryfall_id_by_set_and_number(conn, set_code: str, collector_number: str) -> Optional[str]:
    row = conn.execute(
        """
        SELECT scryfall_id
        FROM card_printings
        WHERE LOWER(set_code) = %s
          AND collector_number = %s
        """,
        (set_code.lower(), collector_number),
    ).fetchone()

    return row["scryfall_id"] if row else None


def get_inventory_row(conn, scryfall_id: str, finish: str):
    return conn.execute(
        """
        SELECT id, stock, override_value
        FROM inventory
        WHERE scryfall_id = %s
          AND finish = %s
        """,
        (scryfall_id, finish),
    ).fetchone()


def set_inventory_stock(conn, scryfall_id: str, finish: str, new_stock: int) -> str:
    existing = get_inventory_row(conn, scryfall_id, finish)

    if existing is None:
        if new_stock <= 0:
            return "unchanged"

        conn.execute(
            """
            INSERT INTO inventory (scryfall_id, finish, stock, override_value, updated_at)
            VALUES (%s, %s, %s, NULL, CURRENT_TIMESTAMP)
            """,
            (scryfall_id, finish, new_stock),
        )
        return "inserted"

    conn.execute(
        """
        UPDATE inventory
        SET stock = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE scryfall_id = %s
          AND finish = %s
        """,
        (new_stock, scryfall_id, finish),
    )
    return "updated"


def change_inventory_stock(conn, scryfall_id: str, finish: str, quantity: int, mode: str) -> str:
    mode = (mode or "set").strip().lower()
    if mode not in {"add", "remove", "set"}:
        raise ValueError(f"Unsupported manual mode: {mode}")

    existing = get_inventory_row(conn, scryfall_id, finish)
    current_stock = int(existing["stock"]) if existing else 0

    if mode == "add":
        new_stock = current_stock + quantity
    elif mode == "remove":
        new_stock = max(0, current_stock - quantity)
    else:
        new_stock = quantity

    return set_inventory_stock(conn, scryfall_id, finish, new_stock)


def process_manual_csv(conn, csv_path: Path, manual_mode: str) -> tuple[int, int]:
    processed = 0
    changed = 0

    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"set_code", "collector_number", "finish", "quantity"}
        headers = {normalize_key(name) for name in (reader.fieldnames or [])}
        missing = required - headers
        if missing:
            raise ValueError(f"Manual CSV missing required columns: {', '.join(sorted(missing))}")

        for line_num, raw_row in enumerate(reader, start=2):
            row = normalized_row(raw_row)
            processed += 1
            savepoint_name = f"manual_row_{line_num}"

            try:
                conn.execute(f"SAVEPOINT {savepoint_name}")

                set_code = row_value(row, "set_code")
                collector_number = row_value(row, "collector_number")
                finish = parse_finish(row_value(row, "finish"))
                quantity = parse_quantity(row_value(row, "quantity", "qty"))
                row_mode = row_value(row, "mode", "action", "operation") or manual_mode

                if not set_code or not collector_number:
                    raise ValueError("set_code and collector_number are required")

                scryfall_id = ensure_card_printing_exists_by_set_and_number(conn, set_code, collector_number)

                result = change_inventory_stock(conn, scryfall_id, finish, quantity, row_mode)
                conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")

                if result != "unchanged":
                    changed += 1

            except Exception as e:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
                #print(f"Manual line {line_num}: skipped, error: {e}")
                rase ValueError(f"Manual line {line_num}: {e}")

    return processed, changed


def process_manabox_csv(conn, csv_path: Path, manabox_mode: str) -> tuple[int, int, int]:
    processed = 0
    changed = 0
    zeroed = 0
    
    manabox_mode = (manabox_mode or "set_listed").strip().lower()
    if manabox_mode not in {"add", "full_sync", "set_listed"}:
        raise ValueError(f"Unsupported ManaBox mode: {manabox_mode}")
    
    if manabox_mode == "full_sync":
        create_temp_seen_table(conn)
    
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = {normalize_key(name) for name in (reader.fieldnames or [])}
        required = {"quantity", "foil"}
        missing = required - headers
        if missing:
            raise ValueError(f"ManaBox CSV missing required columns: {', '.join(sorted(missing))}")

        for line_num, raw_row in enumerate(reader, start=2):
            row = normalized_row(raw_row)
            processed += 1
            savepoint_name = f"manabox_row_{line_num}"

            try:
                conn.execute(f"SAVEPOINT {savepoint_name}")

                scryfall_id = row_value(row, "scryfall_id")
                set_code = row_value(row, "set_code")
                collector_number = row_value(row, "collector_number")

                if not scryfall_id:
                    if not set_code or not collector_number:
                        raise ValueError("ManaBox row needs either Scryfall ID or set_code + collector_number")
                    scryfall_id = get_scryfall_id_by_set_and_number(conn, set_code, collector_number)
                    if scryfall_id is None:
                        raise ValueError(f"Card not found in card_printings for {set_code} #{collector_number}")

                ensure_card_printing_exists(conn, scryfall_id)

                finish = parse_finish(row_value(row, "foil"))
                quantity = parse_quantity(row_value(row, "quantity", "qty"))
                
                if manabox_mode == "add":
                    result = change_inventory_stock(conn, scryfall_id, finish, quantity, "add")
                else:
                    result = set_inventory_stock(conn, scryfall_id, finish, quantity)
                
                if manabox_mode == "full_sync":
                    mark_seen(conn, scryfall_id=scryfall_id, finish=finish)
                
                conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")

                if result != "unchanged":
                    changed += 1

            except Exception as e:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint_name}")
                #print(f"ManaBox line {line_num}: skipped, error: {e}")
                raise ValueError(f"Manual line {line_num}: {e}")

    return processed, changed, zeroed


def main(csv_file: str, import_type: str = "auto", manual_mode: str = "set", manabox_mode: str = "set_listed",) -> None:
    csv_path = Path(csv_file)
    if not csv_path.exists():
        raise FileNotFoundError(f"File not found: {csv_file}")

    with get_connection() as conn:
        try:
            with csv_path.open(newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                import_kind = detect_import_type(reader.fieldnames or [], import_type)

            if import_kind == "manual":
                processed, changed = process_manual_csv(conn, csv_path, manual_mode)
                print(f"Done. Type: {import_kind}. Processed: {processed}. Changed rows: {changed}.")
            else:
                processed, changed, zeroed = process_manabox_csv(conn, csv_path, manabox_mode)
                print(f"Done. Type: {import_kind}. Processed: {processed}. Changed rows: {changed}. Zeroed rows: {zeroed}")

            conn.commit()
            
            
        except Exception:
            conn.rollback()
            raise


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("csv_file")
    parser.add_argument("--import-type", choices=["auto", "manual", "manabox"], default="auto")
    parser.add_argument("--manual-mode", choices=["add", "remove", "set"], default="set")
    parser.add_argument("--manabox-mode", choices=["add", "full_sync", "set_listed"], default="set_listed")
    args = parser.parse_args()

    main(args.csv_file, import_type=args.import_type, manual_mode=args.manual_mode, manabox_mode=args.manabox_mode,)

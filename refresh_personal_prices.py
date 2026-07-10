import os
from typing import Iterable

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


def chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(values), size):
        yield values[i:i + size]


def clean_price(value):
    if value in (None, ""):
        return None
    return float(value)


def fetch_cards_by_scryfall_ids(scryfall_ids: list[str]) -> list[dict]:
    if not scryfall_ids:
        return []

    resp = requests.post(
        "https://api.scryfall.com/cards/collection",
        headers=HEADERS,
        json={"identifiers": [{"id": scryfall_id} for scryfall_id in scryfall_ids]},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    return payload.get("data", [])


def refresh_prices(limit_to_inventory_only: bool = False) -> tuple[int, int]:
    with get_connection() as conn:
        if limit_to_inventory_only:
            rows = conn.execute(
                """
                SELECT DISTINCT cp.scryfall_id
                FROM inventory i
                JOIN card_printings cp
                  ON cp.scryfall_id = i.scryfall_id
                WHERE i.stock > 0
                ORDER BY cp.scryfall_id
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT scryfall_id
                FROM card_printings
                ORDER BY scryfall_id
                """
            ).fetchall()

        scryfall_ids = [row["scryfall_id"] for row in rows]
        updated = 0

        for batch in chunks(scryfall_ids, 50):
            cards = fetch_cards_by_scryfall_ids(batch)

            for card in cards:
                prices = card.get("prices", {}) or {}
                
                price_rows = [
                    ("nonfoil", clean_price(prices.get("usd"))),
                    ("foil", clean_price(prices.get("usd_foil"))),
                    ("etched", clean_price(prices.get("usd_etched"))),
                ]
                
                for finish, market_price in price_rows:
                    if market_price is None:
                        continue
                    
                    conn.execute(
                        """
                        INSERT INTO card_price_snapshots (
                            scryfall_id,
                            finish,
                            market_price,
                            snapshot_at
                        )
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        """,
                        (
                            card.get("id"),
                            finish,
                            market_price
                        )
                    )

                conn.execute(
                    """
                    UPDATE card_printings
                    SET
                        usd_price = %s,
                        usd_foil_price = %s,
                        usd_etched_price = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE scryfall_id = %s
                    """,
                    (
                        clean_price(prices.get("usd")),
                        clean_price(prices.get("usd_foil")),
                        clean_price(prices.get("usd_etched")),
                        card.get("id"),
                    ),
                )
                updated += 1

        conn.commit()
        return len(scryfall_ids), updated


def main(limit_to_inventory_only: bool = False) -> None:
    total, updated = refresh_prices(limit_to_inventory_only=limit_to_inventory_only)
    print(f"Done. Requested: {total}. Updated: {updated}.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--inventory-only",
        action="store_true",
        help="Only refresh prices for card printings that currently have inventory stock.",
    )
    args = parser.parse_args()

    main(limit_to_inventory_only=args.inventory_only)

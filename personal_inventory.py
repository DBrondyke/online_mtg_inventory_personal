import os
import tempfile
from typing import Optional

import pandas as pd
import psycopg
from psycopg.rows import dict_row
import streamlit as st

try:
    from personal_inventory_import import main as run_personal_import
except Exception:
    run_personal_import = None


st.set_page_config(
    page_title="Personal Inventory",
    page_icon="🃏",
    layout="wide",
)

# Replace with your real admin email(s)
ALLOWED_ADMIN_EMAILS = {
    "levibrondyke@gmail.com",
}


def get_database_url() -> str:
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        return db_url
    return st.secrets["database"]["url"]


def get_connection():
    return psycopg.connect(
        get_database_url(),
        row_factory=dict_row,
    )


def is_admin_user() -> bool:
    return (
        st.user.is_logged_in
        and bool(getattr(st.user, "email", None))
        and st.user.email in ALLOWED_ADMIN_EMAILS
    )


def search_inventory(
    *,
    name_query: str,
    set_query: str,
    color_filter: str,
    max_price: Optional[float],
    in_stock_only: bool,
) -> pd.DataFrame:
    sql = """
    WITH grouped_inventory AS (
        SELECT
            MIN(cp.card_name) AS card_name,
            MIN(cp.set_name) AS set_name,
            cp.set_code,
            cp.collector_number,
            MIN(cp.mana_cost) AS mana_cost,
            MIN(cp.color_identity) AS color_identity,
            MIN(cp.type_line) AS type_line,
            MIN(cp.oracle_text) AS oracle_text,
            SUM(i.stock) AS total_stock,
            MIN(
                CASE
                    WHEN i.override_value IS NOT NULL THEN i.override_value
                    WHEN i.finish = 'nonfoil' THEN COALESCE(cp.usd_price, cp.usd_foil_price, cp.usd_etched_price, cp.rarity_floor_value)
                    WHEN i.finish = 'foil' THEN COALESCE(cp.usd_foil_price, cp.usd_price, cp.usd_etched_price, cp.rarity_floor_value)
                    WHEN i.finish = 'etched' THEN COALESCE(cp.usd_etched_price, cp.usd_foil_price, cp.usd_price, cp.rarity_floor_value)
                    ELSE COALESCE(cp.usd_price, cp.usd_foil_price, cp.usd_etched_price, cp.rarity_floor_value)
                END
            ) AS price
        FROM inventory i
        JOIN card_printings cp
          ON cp.scryfall_id = i.scryfall_id
        WHERE i.stock >= 0
    """
    params = []

    if in_stock_only:
        sql += " AND i.stock > 0"

    if name_query.strip():
        sql += " AND cp.card_name ILIKE %s"
        params.append(f"%{name_query.strip()}%")

    if set_query.strip():
        sql += " AND (cp.set_name ILIKE %s OR LOWER(cp.set_code) = %s)"
        params.append(f"%{set_query.strip()}%")
        params.append(set_query.strip().lower())

    if color_filter == "Colorless":
        sql += " AND (cp.color_identity IS NULL OR cp.color_identity = '' OR LOWER(cp.color_identity) = 'colorless')"
    elif color_filter != "All":
        sql += " AND cp.color_identity ILIKE %s"
        params.append(f"%{color_filter}%")

    if max_price is not None:
        sql += """
        AND (
            CASE
                WHEN i.override_value IS NOT NULL THEN i.override_value
                WHEN i.finish = 'nonfoil' THEN COALESCE(cp.usd_price, cp.usd_foil_price, cp.usd_etched_price, cp.rarity_floor_value)
                WHEN i.finish = 'foil' THEN COALESCE(cp.usd_foil_price, cp.usd_price, cp.usd_etched_price, cp.rarity_floor_value)
                WHEN i.finish = 'etched' THEN COALESCE(cp.usd_etched_price, cp.usd_foil_price, cp.usd_price, cp.rarity_floor_value)
                ELSE COALESCE(cp.usd_price, cp.usd_foil_price, cp.usd_etched_price, cp.rarity_floor_value)
            END
        ) <= %s
        """
        params.append(max_price)

    sql += """
        GROUP BY
            cp.set_code,
            cp.collector_number
    )
    SELECT
        card_name,
        set_name,
        set_code,
        collector_number,
        mana_cost,
        color_identity,
        type_line,
        oracle_text,
        total_stock,
        price
    FROM grouped_inventory
    ORDER BY
        card_name,
        set_name,
        CASE
            WHEN collector_number ~ '^[0-9]+$' THEN CAST(collector_number AS INTEGER)
            ELSE 999999
        END,
        collector_number
    """

    with get_connection() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()

    columns = [
        "card_name",
        "set_name",
        "set_code",
        "collector_number",
        "mana_cost",
        "color_identity",
        "type_line",
        "oracle_text",
        "total_stock",
        "price",
    ]

    if not rows:
        return pd.DataFrame(columns=columns)

    return pd.DataFrame([dict(r) for r in rows], columns=columns)


def availability_text(total_stock) -> str:
    try:
        return "In Stock" if int(total_stock) > 0 else "Out of Stock"
    except Exception:
        return "Out of Stock"


def show_admin_panel() -> None:
    st.subheader("Admin Tools")
    st.caption("Only logged-in admin users can see this section.")

    admin_col1, admin_col2 = st.columns([1, 1])

    with admin_col1:
        st.button("Log out", on_click=st.logout, width="content")

    with admin_col2:
        st.write(f"Signed in as: **{st.user.email}**")

    uploaded_file = st.file_uploader(
        "Upload inventory CSV",
        type=["csv"],
        accept_multiple_files=False,
        help="This is intended for spreadsheet uploads into your personal inventory database.",
    )

    if uploaded_file is not None:
        st.write(f"Selected file: **{uploaded_file.name}**")

        if st.button("Run import", type="primary", width="stretch"):
            if run_personal_import is None:
                st.error(
                    "No personal_inventory_import.py module was found. "
                    "Create a file with a main(csv_path: str) function, then redeploy."
                )
                return

            with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
                tmp.write(uploaded_file.getbuffer())
                temp_path = tmp.name

            try:
                run_personal_import(temp_path)
                st.success("Import completed.")
            except Exception as e:
                st.error(f"Import failed: {e}")


header_left, header_right = st.columns([1, 6])

with header_left:
    st.markdown("## 🃏")

with header_right:
    st.title("Personal Inventory")
    st.caption("Public browse view with login-protected admin upload tools")

with st.sidebar:
    if not st.user.is_logged_in:
        st.button("Admin login", on_click=st.login, width="stretch")
        st.caption("Only approved email addresses can access upload tools.")
    else:
        st.success(f"Logged in as {st.user.email}")
        if not is_admin_user():
            st.warning("This account is signed in, but not authorized for admin tools.")

    st.header("Filters")
    name_query = st.text_input("Card name")
    set_query = st.text_input("Set name or code")
    color_filter = st.selectbox(
        "Color",
        ["All", "White", "Blue", "Black", "Red", "Green", "Colorless"],
        index=0,
    )
    max_price_enabled = st.checkbox("Set max price")
    max_price = None
    if max_price_enabled:
        max_price = st.number_input("Max price", min_value=0.0, step=1.0, format="%.2f")
    in_stock_only = st.checkbox("Only show cards in stock", value=True)

results_df = search_inventory(
    name_query=name_query,
    set_query=set_query,
    color_filter=color_filter,
    max_price=max_price,
    in_stock_only=in_stock_only,
)

left, right = st.columns([3, 2])

with left:
    st.write(f"Matches: {len(results_df)}")

    if results_df.empty:
        st.info("No cards match the current filters.")
    else:
        display_df = results_df[
            [
                "card_name",
                "set_name",
                "mana_cost",
                "color_identity",
                "type_line",
                "price",
                "total_stock",
                "set_code",
                "collector_number",
            ]
        ].copy()

        display_df["availability"] = display_df["total_stock"].apply(availability_text)

        event = st.dataframe(
            display_df,
            width="stretch",
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            column_order=[
                "card_name",
                "set_name",
                "mana_cost",
                "color_identity",
                "type_line",
                "price",
                "availability",
                "set_code",
                "collector_number",
            ],
            column_config={
                "card_name": st.column_config.TextColumn("Card Name", width="medium"),
                "set_name": st.column_config.TextColumn("Set", width="medium"),
                "mana_cost": st.column_config.TextColumn("Cost", width="small"),
                "color_identity": st.column_config.TextColumn("Color", width="small"),
                "type_line": st.column_config.TextColumn("Type", width="medium"),
                "price": st.column_config.NumberColumn("Price", format="$%.2f"),
                "availability": st.column_config.TextColumn("Availability", width="small"),
                "set_code": st.column_config.TextColumn("Set Code", width="small"),
                "collector_number": st.column_config.TextColumn("Collector #", width="small"),
            },
        )

with right:
    if results_df.empty:
        st.info("Select a card to view details.")
    else:
        selected_rows = event["selection"]["rows"] if "event" in locals() else []

        if not selected_rows:
            st.info("Select a card to view details.")
        else:
            selected_row = results_df.iloc[selected_rows[0]]

            st.subheader(selected_row["card_name"])
            st.write(f"**Set:** {selected_row['set_name']} ({selected_row['set_code']})")
            st.write(f"**Collector #:** {selected_row['collector_number']}")
            st.write(f"**Cost:** {selected_row['mana_cost'] or '—'}")
            st.write(f"**Color:** {selected_row['color_identity'] or 'Colorless'}")
            st.write(f"**Type:** {selected_row['type_line'] or '—'}")
            st.write(f"**Availability:** {availability_text(selected_row['total_stock'])}")
            st.write(f"**Price:** ${float(selected_row['price']):.2f}" if selected_row["price"] is not None else "**Price:** —")

            st.text_area(
                "Oracle Text",
                value=selected_row["oracle_text"] or "",
                height=220,
                disabled=True,
            )

if is_admin_user():
    st.divider()
    show_admin_panel()

st.caption("Public browse view. Admin import tools require login and approved email.")

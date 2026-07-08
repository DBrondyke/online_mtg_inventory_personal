import os
import tempfile
import hmac
from typing import Optional
from refresh_personal_prices import main as run_price_refresh

import pandas as pd
import psycopg
from psycopg.rows import dict_row
import streamlit as st

COLOR_OPTIONS = ["White", "Blue", "Black", "Red", "Green", "Colorless"]
TYPE_OPTIONS = ["Creature", "Instant", "Sorcery", "Artifact", "Enchantment", "Planeswalker", "Land","Battle"]

COMMON_SUBTYPES = [
    "Angel",
    "Artifact",
    "Assassin",
    "Cat",
    "Cleric",
    "Demon",
    "Dragon",
    "Dinosaur",
    "Eldrazi",
    "Elf",
    "Faerie",
    "Goblin",
    "Human",
    "Knight",
    "Merfolk",
    "Ninja",
    "Pirate",
    "Plant",
    "Rat",
    "Shaman",
    "Sliver",
    "Soldier",
    "Spirit",
    "Treefolk",
    "Vampire",
    "Warrior",
    "Wizard",
    "Zombie",
]


try:
    from personal_inventory_import import main as run_personal_import
except Exception:
    run_personal_import = None


st.set_page_config(
    page_title="Personal Inventory",
    page_icon="🃏",
    layout="wide",
)

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


def password_check() -> bool:
    if st.session_state.get("admin_authenticated", False):
        return True
    
    def try_login():
        entered = st.session_state.get("admin_password_input", "")
        expected = st.secrets["admin"]["password"]
        
        if hmac.compare_digest(entered, expected):
            st.session_state["admin_authenticated"] = True
            st.session_state["admin_password_input"] = ""
            st.session_state["admin_login_error"] = ""
        else:
            st.session_state["admin_authenticated"] = False
            st.session_state["admin_login_error"] = "Incorrect password."
    
    st.subheader("Admin Access")
    st.text_input(
        "Admin password",
        type="password",
        key="admin_password_input",
    )
    st.button("Unlock admin tools", on_click=try_login, width="stretch")
    
    if st.session_state.get("admin_login_error"):
        st.error(st.session_state["admin_login_error"])
    
    return False

def admin_logout():
    st.session_state["admin_authenticated"] = False
    st.session_state["admin_password_input"] = ""
    st.session_state["admin_login_error"] = ""

def build_color_clause(colors: List[str], mode: str) -> tuple[str, list]:
    if not colors:
        return "", []

    clauses = []
    params = []

    if mode == "Exactly these colors":
        if len(colors) == 5:
            exact_value = "All"
        else:
            exact_value = "/".join(colors)
        return " AND cp.color_identity = %s", [exact_value]

    if mode == "Contains all selected colors":
        for color in colors:
            clauses.append("cp.color_identity LIKE %s")
            params.append(f"%{color}%")
        return " AND " + " AND ".join(clauses), params

    if mode == "Commander identity":
        excluded_colors = [c for c in COLOR_OPTIONS if c not in colors]
        for color in excluded_colors:
            clauses.append("(cp.color_identity IS NULL OR cp.color_identity = '' OR cp.color_identity NOT LIKE %s)")
            params.append(f"%{color}%")
        return " AND " + " AND ".join(clauses), params

    if mode == "Contains any selected color":
        for color in colors:
            clauses.append("cp.color_identity LIKE %s")
            params.append(f"%{color}%")
        return " AND (" + " OR ".join(clauses) + ")", params

    return "", []


def build_type_clause(types: List[str], mode: str) -> tuple[str, list]:
    if not types:
        return "", []

    clauses = []
    params = []

    if mode == "Must include all selected types":
        for t in types:
            clauses.append("cp.type_line LIKE %s")
            params.append(f"%{t}%")
        return " AND " + " AND ".join(clauses), params

    for t in types:
        clauses.append("cp.type_line LIKE %s")
        params.append(f"%{t}%")
    return " AND (" + " OR ".join(clauses) + ")", params

# inventory table helper
def render_inventory_table(df: pd.DataFrame):
    return st.dataframe(
        df,
        key="inventory_table",
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_order=[
            "card_name",
            "set_name",
            "mana_cost_display",
            "color_identity_display",
            "type_line",
            "price",
            "stock_count",
            "set_code",
            "collector_number",
        ],
        column_config={
            "card_name": st.column_config.TextColumn("Card Name", width="medium"),
            "set_name": st.column_config.TextColumn("Set", width="medium"),
            "mana_cost_display": st.column_config.TextColumn("Cost", width="small"),
            "color_identity_display": st.column_config.TextColumn("Color", width="small"),
            "type_line": st.column_config.TextColumn("Type", width="medium"),
            "price": st.column_config.NumberColumn("Price", format="$%.2f"),
            "stock_count": st.column_config.TextColumn("Stock", width="small"),
            "set_code": st.column_config.TextColumn("Set Code", width="small"),
            "collector_number": st.column_config.TextColumn("Collector #", width="small"),
        },
    )

def get_selected_rows() -> list[int]:
    table_state = st.session_state.get("inventory_table", {})
    selection = table_state.get("selection", {})
    rows = selection.get("rows", [])
    return rows if isinstance(rows, list) else []


def clean_text(value, fallback="-"):
    if value is None:
        return fallback
    text = str(value).strip()
    if text == "" or text.lower() == "none" or text.lower() == "nan":
        return fallback
    return text

def search_inventory(
    *,
    name_query: str,
    set_query: str,
    oracle_query: str,
    selected_types: list[str],
    type_mode: str,
    selected_subtypes: list[str],
    subtype_text: str,
    subtype_mode: str,
    min_stock: int,
    color_filter: str,
    color_mode: str,
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
            MIN(cp.standard_legal) AS standard_legal,
            MIN(cp.commander_legal) AS commander_legal,
            MIN(cp.color_identity) AS color_identity,
            MIN(cp.mana_cost) AS mana_cost,
            MIN(cp.mana_value) AS mana_value,
            MIN(cp.type_line) AS type_line,
            MIN(cp.oracle_text) AS oracle_text,
            SUM(i.stock) AS total_stock,
            ROUND(
                MIN(
                    CASE
                        WHEN i.override_value IS NOT NULL THEN i.override_value
                        WHEN i.finish = 'nonfoil' THEN COALESCE(cp.usd_price, cp.usd_foil_price, cp.usd_etched_price, cp.rarity_floor_value)
                        WHEN i.finish = 'foil' THEN COALESCE(cp.usd_foil_price, cp.usd_price, cp.usd_etched_price, cp.rarity_floor_value)
                        WHEN i.finish = 'etched' THEN COALESCE(cp.usd_etched_price, cp.usd_foil_price, cp.usd_price, cp.rarity_floor_value)
                        ELSE COALESCE(cp.usd_price, cp.usd_foil_price, cp.usd_etched_price, cp.rarity_floor_value)
                    END
                ),
                2
            ) AS price
        FROM inventory i
        JOIN card_printings cp
          ON cp.scryfall_id = i.scryfall_id
        WHERE 1=1
    """
    params: list = []

    if in_stock_only:
        sql += " AND i.stock > 0"

    if min_stock > 0:
        sql += " AND i.stock >= %s"
        params.append(min_stock)

    if name_query.strip():
        sql += " AND cp.card_name ILIKE %s"
        params.append(f"%{name_query.strip()}%")

    if oracle_query.strip():
        words = [w.strip() for w in oracle_query.split() if w.strip()]
        for word in words:
            sql += " AND cp.oracle_text ILIKE %s"
            params.append(f"%{word}%")

    if set_query.strip():
        sql += " AND (cp.set_name ILIKE %s OR LOWER(cp.set_code) = %s)"
        params.append(f"%{set_query.strip()}%")
        params.append(set_query.strip().lower())

    if color_filter == "Colorless":
        sql += " AND (cp.color_identity IS NULL OR cp.color_identity = '' OR LOWER(cp.color_identity) = 'colorless')"
    elif color_filter != "All":
        sql += " AND cp.color_identity ILIKE %s"
        params.append(f"%{color_filter}%")

    if selected_types:
        if type_mode == "Must include all selected types":
            for t in selected_types:
                sql += " AND cp.type_line ILIKE %s"
                params.append(f"%{t}%")
        else:
            type_clauses = []
            for t in selected_types:
                type_clauses.append("cp.type_line ILIKE %s")
                params.append(f"%{t}%")
            sql += " AND (" + " OR ".join(type_clauses) + ")"

    subtype_terms = list(selected_subtypes)
    if subtype_text.strip():
        subtype_terms.extend([s.strip() for s in subtype_text.split(",") if s.strip()])

    if subtype_terms:
        if subtype_mode == "Must include all selected subtypes":
            for s in subtype_terms:
                sql += " AND cp.type_line ILIKE %s"
                params.append(f"%{s}%")
        else:
            subtype_clauses = []
            for s in subtype_terms:
                subtype_clauses.append("cp.type_line ILIKE %s")
                params.append(f"%{s}%")
            sql += " AND (" + " OR ".join(subtype_clauses) + ")"

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

def show_admin_panel() -> None:
    st.subheader("Admin Tools")
    st.caption("Only logged-in admin users can see this section.")
    
    import_type_label = st.selectbox(
        "Import type",
        ["Manual stock CSV", "ManaBox Export"],
    )
    
    manual_mode = "set"
    manabox_mode = "set_listed"
    
    if import_type_label == "Manual stock CSV":
        manual_mode_label = st.selectbox(
            "Manual mode",
            ["Set", "Add", "Remove"],
        )
        manual_mode = manual_mode_label.lower()
    else:
        manabox_mode_label = st.selectbox(
            "ManaBox mode",
            [
                "Add contents of CSV",
                "Full inventory sync",
            ],
        )
        
        if manabox_mode_label == "Add contents of CSV":
            manabox_mode = "add"
        else:
            manabox_mode = "full_sync"
       
    import_type = "manual" if import_type_label == "Manual stock CSV" else "manabox"

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
                run_personal_import(temp_path, import_type=import_type, manual_mode=manual_mode, manabox_mode=manabox_mode,)
                st.success("Import completed.")
            except Exception as e:
                st.error(f"Import failed: {e}")
    
    st.subheader("Price Refresh")
    
    inventory_only = st.checkbox(
        "Only refresh prices for cards currently in stock",
        value=True,
    )
    
    if st.button("Refresh prices", width="stretch"):
        try:
            run_price_refresh(limit_to_inventory_only=inventory_only)
            st.success("Price refresh completed.")
        except Exception as e:
            st.error(f"Price refresh failed: {e}")


header_left, header_right = st.columns([1, 6])

with header_left:
    st.markdown("## 🃏")

with header_right:
    st.title("Dustin B's Inventory")
    st.caption("Public browse view with login-protected admin upload tools")

with st.sidebar:
    st.header("Filters")
    name_query = st.text_input("Card name")
    oracle_query = st.text_input("Oracle text contains")
    set_query = st.text_input("Set name or code")
    color_filter = st.selectbox(
        "Color",
        ["All", "White", "Blue", "Black", "Red", "Green", "Colorless"],
        index=0,
    )
    color_mode = st.selectbox(
        "Color match mode",
        [
            "Contains all selected colors",
            "Contains any selected color",
            "Exactly these colors",
            "Commander identity",
        ],
        index=0,
    )
    selected_types = st.multiselect("Type contains", TYPE_OPTIONS)
    type_mode = st.selectbox(
        "Type match mode",
        ["Must include any selected type", "Must include all selected types"],
        index=0,
    )

    selected_subtypes = st.multiselect("Subtype contains", COMMON_SUBTYPES)
    subtype_text = st.text_input("Extra subtypes (comma-separated)")
    subtype_mode = st.selectbox(
        "Subtype match mode",
        ["Must include any selected subtype", "Must include all selected subtypes"],
        index=0,
    )
    max_price_enabled = st.checkbox("Set max price")
    max_price = None
    if max_price_enabled:
        max_price = st.number_input("Max price", min_value=0.0, step=1.0, format="%.2f")
    in_stock_only = st.checkbox("Only show cards in stock", value=True)
    min_stock = st.number_input("Minimum stock", min_value=0, value=0, step=1)
    st.divider()
    st.subheader("Admin Access")
    if st.session_state.get("admin_authenticated", False):
        st.success("Admin tools unlocked")
        st.button("Lock admin tools", on_click=admin_logout, width="stretch")
    else:
        password_check()
        st.caption("Only someone with the admin password can upload or change inventory.")

results_df = search_inventory(
    name_query=name_query,
    set_query=set_query,
    color_filter=color_filter,
    max_price=max_price,
    in_stock_only=in_stock_only,
    oracle_query=oracle_query,
    color_mode=color_mode,
    selected_types=selected_types,
    type_mode=type_mode,
    selected_subtypes=selected_subtypes,
    subtype_text=subtype_text,
    subtype_mode=subtype_mode,
    min_stock=min_stock,
)

left, right = st.columns([5,2])

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
                "oracle_text",
                "price",
                "total_stock",
                "set_code",
                "collector_number",
            ]
        ].copy()
        
        display_df["stock_count"] = display_df["total_stock"].fillna(0).astype(int)
        display_df["mana_cost_display"] = display_df["mana_cost"].apply(lambda v: clean_text(v, "-"))
        display_df["color_identity_display"] = display_df["color_identity"].apply(lambda v: clean_text(v, "Colorless"))
        display_df["oracle_text_display"] = display_df["oracle_text"].apply(lambda v: clean_text(v, "-"))
        
        event = render_inventory_table(display_df)
    
with right:
    if results_df.empty:
        st.write("Select a card to view details.")
    else:
        selected_rows = get_selected_rows()
        
        # Guard against stale selection after filters change
        if selected_rows and selected_rows[0] >= len(results_df):
            selected_rows = []
    
        #Put above match count
        if not selected_rows:
            st.write("Select a card to view details.")
        else:
            selected_row = results_df.iloc[selected_rows[0]]
            
            st.subheader(selected_row["card_name"])
            
            st.write(f"**Set:** {selected_row['set_name']} ({selected_row['set_code']})")
            st.write(f"**Collector #:** {selected_row['collector_number']}")
            st.write(f"**Cost:** {clean_text(selected_row['mana_cost'],'-')}")
            st.write(f"**Color:** {clean_text(selected_row['color_identity'],'Colorless')}")
            st.write(f"**Type:** {selected_row['type_line'] or '-'}")
            st.write(f"**Stock:** {int(selected_row['total_stock'])}")
            st.write(
                f"**Price:** ${float(selected_row['price']):.2f}"
                if selected_row["price"] is not None 
                else "**Price:** -"
            )

            st.text_area(
                "Oracle Text",
                value=clean_text(selected_row["oracle_text"],"-"),
                height=220,
                disabled=True,
            )

if st.session_state.get("admin_authenticated", False):
    st.divider()
    show_admin_panel()

st.caption("Public browse view. Admin import tools require login and approved email.")

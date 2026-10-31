"""Loading and cleaning of the store's operational CSVs.

The CSVs are small (< 100 rows each), so they are loaded once into pandas DataFrames and queried
in memory. Each cleaning step below exists because of something observed in the raw data — see
"Data treatment" in the README.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pandas as pd

from .config import DATA_RAW


def normalize_text(text: str) -> str:
    """Lowercase, strip accents and collapse punctuation — used for search matching only."""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return text.strip()


def only_digits(text: str) -> str:
    return re.sub(r"\D", "", str(text or ""))


def _parse_specs(raw: str) -> dict:
    try:
        specs = json.loads(raw)
        return specs if isinstance(specs, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


@dataclass
class StoreData:
    products: pd.DataFrame
    categories: pd.DataFrame
    promotions: pd.DataFrame
    orders: pd.DataFrame
    order_items: pd.DataFrame
    customers: pd.DataFrame

    @classmethod
    def load(cls, data_dir: Path = DATA_RAW) -> "StoreData":
        categories = pd.read_csv(data_dir / "categories.csv")
        products = pd.read_csv(data_dir / "products.csv")
        promotions = pd.read_csv(data_dir / "promotions.csv")
        orders = pd.read_csv(data_dir / "orders.csv", dtype={"tracking_code": "string", "notes": "string"})
        order_items = pd.read_csv(data_dir / "order_items.csv")
        customers = pd.read_csv(data_dir / "customers.csv", dtype=str)

        # --- products -----------------------------------------------------------------------
        products["specs"] = products["specs"].map(_parse_specs)
        products["status"] = products["status"].str.strip().str.lower()
        products = products.merge(
            categories[["category_id", "name"]].rename(columns={"name": "category"}),
            on="category_id",
            how="left",
        )
        # Only *active* promotions count (is_active == 1). A product may have several promotion
        # rows (e.g. product 96); if more than one were active we keep the largest discount,
        # because promotions are not cumulative (policy 6.2).
        active = (
            promotions[promotions["is_active"] == 1]
            .sort_values("discount_percent", ascending=False)
            .drop_duplicates("product_id")
            .rename(columns={"description": "promo_name", "discount_percent": "promo_discount"})
        )
        products = products.merge(
            active[["product_id", "promo_discount", "promo_name"]], on="product_id", how="left"
        )
        products["promo_price"] = (
            products["price_brl"] * (1 - products["promo_discount"] / 100)
        ).round(2)
        products["search_text"] = (
            products["name"] + " " + products["category"] + " " + products["description"]
            + " " + products["specs"].map(lambda s: " ".join(map(str, s.values())))
        ).map(normalize_text)
        products["name_norm"] = products["name"].map(normalize_text)
        products = products.sort_values("product_id").reset_index(drop=True)

        # --- orders / customers -------------------------------------------------------------
        for col in ("order_date", "estimated_delivery"):
            orders[col] = pd.to_datetime(orders[col], errors="coerce").dt.date
        orders["status"] = orders["status"].str.strip().str.lower()
        customers["customer_id"] = customers["customer_id"].astype(int)
        customers["phone_digits"] = customers["phone"].map(only_digits)
        customers["email_norm"] = customers["email"].str.strip().str.lower()

        return cls(products, categories, promotions, orders, order_items, customers)


@lru_cache(maxsize=1)
def get_store_data() -> StoreData:
    return StoreData.load()

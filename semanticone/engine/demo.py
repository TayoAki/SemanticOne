"""A deterministic sample warehouse (DuckDB) and a matching Ossie model.

Every new workspace gets this so people can query real numbers within a minute
of signing up, before connecting their own warehouse.
"""

from __future__ import annotations

import csv
import os
import random
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

DEMO_VERSION = 3  # bump to regenerate the file when the data shape changes

COUNTRIES = ["US", "US", "US", "GB", "DE", "FR", "CA", "AU", "JP", "BR"]
SEGMENTS = ["Consumer", "Consumer", "Small Business", "Enterprise"]
CHANNELS = ["web", "web", "mobile", "marketplace", "retail"]
STATUSES = ["completed"] * 8 + ["returned", "cancelled"]
CATEGORIES = {
    "Audio": ["Wireless Earbuds", "Studio Headphones", "Smart Speaker", "Soundbar"],
    "Computers": ["Ultrabook 14", "Gaming Laptop", "4K Monitor", "Mechanical Keyboard", "Docking Station"],
    "Phones": ["Phone Pro", "Phone Lite", "Phone Case", "Fast Charger"],
    "Home": ["Robot Vacuum", "Smart Thermostat", "Air Purifier", "Video Doorbell"],
    "Wearables": ["Smartwatch", "Fitness Band", "Smart Ring"],
}
BASE_PRICES = {
    "Wireless Earbuds": 129, "Studio Headphones": 299, "Smart Speaker": 99, "Soundbar": 349,
    "Ultrabook 14": 1299, "Gaming Laptop": 1899, "4K Monitor": 449, "Mechanical Keyboard": 149,
    "Docking Station": 229, "Phone Pro": 1099, "Phone Lite": 599, "Phone Case": 39, "Fast Charger": 29,
    "Robot Vacuum": 499, "Smart Thermostat": 199, "Air Purifier": 249, "Video Doorbell": 179,
    "Smartwatch": 399, "Fitness Band": 99, "Smart Ring": 299,
}


def _bulk_insert(con: duckdb.DuckDBPyConnection, table: str, rows: list[tuple]) -> None:
    """Load rows through a temporary CSV file; far faster than executemany in DuckDB."""
    fd, path = tempfile.mkstemp(suffix=".csv")
    try:
        with os.fdopen(fd, "w", newline="") as handle:
            csv.writer(handle).writerows(rows)
        con.execute(f"COPY {table} FROM '{path}' (FORMAT csv, HEADER false)")
    finally:
        os.unlink(path)


def _generate(con: duckdb.DuckDBPyConnection) -> None:
    rng = random.Random(20260101)
    con.execute("CREATE SCHEMA shop")
    con.execute(
        "CREATE TABLE shop.customers (customer_id INTEGER PRIMARY KEY, full_name VARCHAR, email VARCHAR, "
        "country VARCHAR, segment VARCHAR, signup_date DATE)"
    )
    con.execute(
        "CREATE TABLE shop.products (product_id INTEGER PRIMARY KEY, product_name VARCHAR, category VARCHAR, "
        "list_price DECIMAL(10,2), unit_cost DECIMAL(10,2))"
    )
    con.execute(
        "CREATE TABLE shop.orders (order_id INTEGER PRIMARY KEY, customer_id INTEGER, ordered_at TIMESTAMP, "
        "status VARCHAR, channel VARCHAR, shipping_fee DECIMAL(10,2))"
    )
    con.execute(
        "CREATE TABLE shop.order_items (order_item_id INTEGER PRIMARY KEY, order_id INTEGER, product_id INTEGER, "
        "quantity INTEGER, unit_price DECIMAL(10,2), discount_pct DECIMAL(5,2))"
    )
    con.execute(
        "CREATE TABLE shop.support_tickets (ticket_id INTEGER PRIMARY KEY, customer_id INTEGER, opened_at TIMESTAMP, "
        "priority VARCHAR, topic VARCHAR, satisfaction INTEGER, resolution_hours DECIMAL(8,1))"
    )

    first_names = ["Ada", "Grace", "Alan", "Linus", "Margaret", "Ken", "Barbara", "Dennis", "Radia", "Tim",
                   "Frances", "Edsger", "Katherine", "John", "Hedy", "Guido", "Anita", "Donald", "Sophie", "Yukihiro"]
    last_names = ["Lovelace", "Hopper", "Turing", "Torvalds", "Hamilton", "Thompson", "Liskov", "Ritchie",
                  "Perlman", "Berners-Lee", "Allen", "Dijkstra", "Johnson", "McCarthy", "Lamarr", "van Rossum",
                  "Borg", "Knuth", "Wilson", "Matsumoto"]
    start = date(2024, 1, 1)
    end = date(2026, 9, 30)
    span = (end - start).days

    customers = []
    for cid in range(1, 1201):
        name = f"{rng.choice(first_names)} {rng.choice(last_names)}"
        signup = start + timedelta(days=int(span * rng.random() ** 1.3))
        customers.append(
            (cid, name, f"customer{cid}@example.com", rng.choice(COUNTRIES), rng.choice(SEGMENTS), signup)
        )
    _bulk_insert(con, "shop.customers", customers)

    products = []
    pid = 0
    for category, names in CATEGORIES.items():
        for name in names:
            pid += 1
            price = BASE_PRICES[name]
            products.append((pid, name, category, price, round(price * rng.uniform(0.45, 0.7), 2)))
    _bulk_insert(con, "shop.products", products)

    orders, items, tickets = [], [], []
    item_id = 0
    order_id = 0
    for cid, *_rest, signup in customers:
        days_active = (end - signup).days
        n_orders = max(0, int(rng.gauss(1 + days_active / 120, 2)))
        for _ in range(n_orders):
            order_id += 1
            # Seasonality: more orders late in the year.
            day = signup + timedelta(days=rng.randint(0, max(days_active, 0)))
            if rng.random() < 0.25 and day.month < 11:
                holiday = day.replace(month=rng.choice([11, 12]), day=min(day.day, 30))
                if holiday <= end:
                    day = holiday
            ts = datetime(day.year, day.month, day.day, rng.randint(0, 23), rng.randint(0, 59))
            orders.append(
                (order_id, cid, ts, rng.choice(STATUSES), rng.choice(CHANNELS), rng.choice([0, 0, 4.99, 9.99]))
            )
            for _ in range(rng.choice([1, 1, 1, 2, 2, 3, 4])):
                item_id += 1
                product = rng.choice(products)
                discount = rng.choice([0, 0, 0, 0.05, 0.1, 0.2])
                price = round(float(product[3]) * rng.uniform(0.95, 1.05), 2)
                items.append((item_id, order_id, product[0], rng.choice([1, 1, 1, 2, 3]), price, discount))
        for t in range(int(rng.expovariate(1 / 1.2))):
            day = signup + timedelta(days=rng.randint(0, max(days_active, 0)))
            tickets.append(
                (
                    len(tickets) + 1,
                    cid,
                    datetime(day.year, day.month, day.day, rng.randint(0, 23)),
                    rng.choice(["low", "low", "normal", "normal", "high", "urgent"]),
                    rng.choice(["shipping", "billing", "returns", "technical", "account"]),
                    rng.choice([1, 2, 3, 4, 4, 5, 5, 5]),
                    round(rng.expovariate(1 / 18), 1),
                )
            )
    _bulk_insert(con, "shop.orders", orders)
    _bulk_insert(con, "shop.order_items", items)
    _bulk_insert(con, "shop.support_tickets", tickets)
    con.execute(f"CREATE TABLE shop._meta AS SELECT {DEMO_VERSION} AS version")


def ensure_demo_database(data_dir: Path) -> Path:
    """Create the sample warehouse file once (atomically) and return its path."""
    path = data_dir / f"demo_v{DEMO_VERSION}.duckdb"
    if path.exists():
        return path
    data_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=data_dir, suffix=".tmp.duckdb")
    os.close(fd)
    os.unlink(tmp)
    con = duckdb.connect(tmp)
    try:
        _generate(con)
        con.execute("CHECKPOINT")
    finally:
        con.close()
    try:
        os.replace(tmp, path)
    except OSError:  # another worker won the race
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


DEMO_MODEL_YAML = """\
version: 0.2.0.dev0
name: Acme Commerce
description: >
  Sample e-commerce model: customers place orders containing order items for products,
  and open support tickets. Revenue is measured on order items, net of discounts.
ai_context:
  instructions: >
    Use total_revenue for sales questions. Revenue excludes shipping fees and cancelled orders
    are already removed. "Customers" means accounts in the customers table.
datasets:
  - name: customers
    source: shop.customers
    primary_key: [customer_id]
    description: One row per customer account.
    ai_context:
      synonyms: [accounts, buyers, users]
    fields:
      - name: customer_id
        expression: {sql: customer_id}
        datatype: Integer
      - name: country
        expression: {sql: country}
        datatype: String
        dimension: {}
        description: ISO country code of the billing address.
        ai_context: {synonyms: [market, region, geo]}
      - name: segment
        expression: {sql: segment}
        datatype: String
        dimension: {}
        description: Consumer, Small Business or Enterprise.
      - name: signup_date
        expression: {sql: signup_date}
        datatype: Date
        dimension: {is_time: true}
  - name: products
    source: shop.products
    primary_key: [product_id]
    description: Product catalog.
    fields:
      - name: product_id
        expression: {sql: product_id}
        datatype: Integer
      - name: product_name
        expression: {sql: product_name}
        datatype: String
        dimension: {}
      - name: category
        expression: {sql: category}
        datatype: String
        dimension: {}
        ai_context: {synonyms: [product line, department]}
  - name: orders
    source: shop.orders
    primary_key: [order_id]
    description: One row per order. Revenue and order metrics exclude cancelled orders.
    fields:
      - name: order_id
        expression: {sql: order_id}
        datatype: Integer
      - name: customer_id
        expression: {sql: customer_id}
        datatype: Integer
      - name: ordered_at
        expression: {sql: ordered_at}
        datatype: DateTime
        dimension: {is_time: true}
        description: When the order was placed.
      - name: status
        expression: {sql: status}
        datatype: String
        dimension: {}
      - name: channel
        expression: {sql: channel}
        datatype: String
        dimension: {}
        ai_context: {synonyms: [sales channel, source]}
      - name: is_cancelled
        expression: {sql: "status = 'cancelled'"}
        datatype: Boolean
        dimension: {}
      - name: shipping_fee
        expression: {sql: shipping_fee}
        datatype: Decimal
    metrics:
      - name: shipping_revenue
        expression: {sql: "SUM(CASE WHEN NOT is_cancelled THEN shipping_fee ELSE 0 END)"}
        datatype: Decimal
        description: Shipping fees collected on non-cancelled orders.
  - name: order_items
    source: shop.order_items
    primary_key: [order_item_id]
    description: One row per product line on an order.
    fields:
      - name: order_item_id
        expression: {sql: order_item_id}
        datatype: Integer
      - name: order_id
        expression: {sql: order_id}
        datatype: Integer
      - name: product_id
        expression: {sql: product_id}
        datatype: Integer
      - name: quantity
        expression: {sql: quantity}
        datatype: Integer
      - name: net_amount
        expression: {sql: "quantity * unit_price * (1 - discount_pct)"}
        datatype: Decimal
        description: Line revenue after discount.
    metrics:
      - name: units_sold
        expression: {sql: SUM(quantity)}
        datatype: Integer
  - name: support_tickets
    source: shop.support_tickets
    primary_key: [ticket_id]
    description: Customer support tickets.
    fields:
      - name: ticket_id
        expression: {sql: ticket_id}
        datatype: Integer
      - name: customer_id
        expression: {sql: customer_id}
        datatype: Integer
      - name: opened_at
        expression: {sql: opened_at}
        datatype: DateTime
        dimension: {is_time: true}
      - name: priority
        expression: {sql: priority}
        datatype: String
        dimension: {}
      - name: topic
        expression: {sql: topic}
        datatype: String
        dimension: {}
      - name: satisfaction
        expression: {sql: satisfaction}
        datatype: Integer
        description: CSAT score from 1 to 5.
      - name: resolution_hours
        expression: {sql: resolution_hours}
        datatype: Decimal
relationships:
  - name: order_items_to_orders
    from: order_items
    to: orders
    from_columns: [order_id]
    to_columns: [order_id]
  - name: order_items_to_products
    from: order_items
    to: products
    from_columns: [product_id]
    to_columns: [product_id]
  - name: orders_to_customers
    from: orders
    to: customers
    from_columns: [customer_id]
    to_columns: [customer_id]
  - name: support_tickets_to_customers
    from: support_tickets
    to: customers
    from_columns: [customer_id]
    to_columns: [customer_id]
metrics:
  - name: total_revenue
    expression:
      sql: "SUM(CASE WHEN NOT orders.is_cancelled THEN order_items.net_amount ELSE 0 END)"
    datatype: Decimal
    description: Product revenue net of discounts, excluding cancelled orders.
    ai_context:
      synonyms: [sales, revenue, GMV, net sales]
  - name: order_count
    expression: {sql: "COUNT(DISTINCT CASE WHEN NOT orders.is_cancelled THEN orders.order_id END)"}
    datatype: Integer
    description: Number of non-cancelled orders.
    ai_context: {synonyms: [orders, number of orders]}
  - name: average_order_value
    expression:
      sql: >-
        SUM(CASE WHEN NOT orders.is_cancelled THEN order_items.net_amount ELSE 0 END)
        / NULLIF(COUNT(DISTINCT CASE WHEN NOT orders.is_cancelled THEN orders.order_id END), 0)
    datatype: Decimal
    description: Revenue divided by non-cancelled orders.
    ai_context: {synonyms: [AOV, basket size]}
  - name: customer_count
    expression: {sql: COUNT(DISTINCT customers.customer_id)}
    datatype: Integer
    description: Distinct customers.
  - name: ticket_count
    expression: {sql: COUNT(support_tickets.ticket_id)}
    datatype: Integer
    description: Support tickets opened.
  - name: avg_satisfaction
    expression: {sql: AVG(support_tickets.satisfaction)}
    datatype: Decimal
    description: Average CSAT (1-5).
    ai_context: {synonyms: [CSAT, customer satisfaction]}
  - name: return_rate
    expression:
      sql: "COUNT(DISTINCT CASE WHEN orders.status = 'returned' THEN orders.order_id END) * 1.0 / NULLIF(COUNT(DISTINCT orders.order_id), 0)"
    datatype: Decimal
    description: Share of orders that were returned.
"""

"""Download UCI Online Retail and normalize invoice lines into four tables."""
from pathlib import Path
from urllib.request import urlretrieve
from zipfile import ZipFile
import argparse
import math
from openpyxl import load_workbook
from sqlalchemy import delete
from app.db import writer_engine
from app.init_db import Base
from app.models import Customer, Order, OrderItem, Product

URL = "https://archive.ics.uci.edu/static/public/352/online+retail.zip"
DATA = Path(__file__).resolve().parents[2] / "data" / "raw"

def main(limit: int | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=limit, help="Optional number of source rows for a quick demo")
    args = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    archive = DATA / "online-retail.zip"
    if not archive.exists():
        print(f"Downloading UCI dataset to {archive} ...")
        urlretrieve(URL, archive)
    with ZipFile(archive) as zf:
        member = next(n for n in zf.namelist() if n.lower().endswith(".xlsx"))
        xlsx = DATA / Path(member).name
        if not xlsx.exists(): xlsx.write_bytes(zf.read(member))
    wb = load_workbook(xlsx, read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    headers = [str(v).strip() for v in next(rows)]
    index = {name: headers.index(name) for name in ["InvoiceNo", "StockCode", "Description", "Quantity", "InvoiceDate", "UnitPrice", "CustomerID", "Country"]}
    Base.metadata.create_all(writer_engine)
    customers, products, orders, items = {}, {}, {}, []
    accepted = 0
    for row in rows:
        if args.limit is not None and accepted >= args.limit: break
        try:
            raw_invoice, raw_code = row[index["InvoiceNo"]], row[index["StockCode"]]
            if raw_invoice is None or raw_code is None: continue
            invoice, code, desc = str(raw_invoice).strip(), str(raw_code).strip(), row[index["Description"]]
            quantity, invoice_date, price = int(row[index["Quantity"]]), row[index["InvoiceDate"]], float(row[index["UnitPrice"]])
            if not invoice or not code or not invoice_date or not math.isfinite(price) or price < 0: continue
            raw_customer = row[index["CustomerID"]]
            customer_id = str(int(raw_customer)) if raw_customer is not None else None
            country = str(row[index["Country"]]).strip() if row[index["Country"]] else None
            if customer_id: customers[customer_id] = {"customer_id": customer_id, "country": country}
            products[code] = {"stock_code": code, "description": str(desc).strip() if desc else None}
            cancelled = invoice.upper().startswith("C")
            orders[invoice] = {"invoice_no": invoice, "customer_id": customer_id, "invoice_date": invoice_date, "is_cancelled": cancelled}
            items.append({"invoice_no": invoice, "stock_code": code, "quantity": quantity, "unit_price": price})
            accepted += 1
        except (TypeError, ValueError, OverflowError):
            continue
    # Incomplete rows do not produce customer records; orders remain valid with an unknown customer.
    with writer_engine.begin() as conn:
        for model in [OrderItem, Order, Customer, Product]: conn.execute(delete(model))
        conn.execute(Customer.__table__.insert(), list(customers.values()))
        conn.execute(Product.__table__.insert(), list(products.values()))
        conn.execute(Order.__table__.insert(), list(orders.values()))
        for start in range(0, len(items), 5000):
            conn.execute(OrderItem.__table__.insert(), items[start:start + 5000])
        conn.execute(__import__("sqlalchemy").text("GRANT SELECT ON ALL TABLES IN SCHEMA public TO sqlpilot_reader"))
    wb.close()
    print(f"Imported {accepted:,} invoice rows: {len(customers):,} customers, {len(orders):,} orders, {len(products):,} products, {len(items):,} order items.")

if __name__ == "__main__": main()

from app.models import Base


def test_retail_schema_has_expected_tables_and_links():
    assert set(Base.metadata.tables) == {"customers", "orders", "products", "order_items"}
    orders = Base.metadata.tables["orders"]
    items = Base.metadata.tables["order_items"]
    assert orders.c.customer_id.foreign_keys
    assert items.c.invoice_no.foreign_keys
    assert items.c.stock_code.foreign_keys
    assert orders.c.customer_id.nullable

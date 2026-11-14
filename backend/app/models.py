from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase): pass

class Customer(Base):
    __tablename__ = "customers"
    customer_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    country: Mapped[str] = mapped_column(String(100), nullable=True)
    orders: Mapped[list["Order"]] = relationship(back_populates="customer")

class Product(Base):
    __tablename__ = "products"
    stock_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    description: Mapped[str] = mapped_column(String(500), nullable=True)

class Order(Base):
    __tablename__ = "orders"
    invoice_no: Mapped[str] = mapped_column(String(32), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.customer_id"), index=True, nullable=True)
    invoice_date: Mapped[object] = mapped_column(DateTime, index=True)
    is_cancelled: Mapped[bool] = mapped_column(default=False)
    customer: Mapped[Customer] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")

class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    invoice_no: Mapped[str] = mapped_column(ForeignKey("orders.invoice_no"), index=True)
    stock_code: Mapped[str] = mapped_column(ForeignKey("products.stock_code"), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[object] = mapped_column(Numeric(12, 2))
    order: Mapped[Order] = relationship(back_populates="items")

from pydantic import BaseModel


class StoreInfo(BaseModel):
    name: str
    url: str
    currency: str
    timezone: str
    now: str
    today: str


class Product(BaseModel):
    id: int
    name: str
    sku: str
    status: str
    price: str
    regular_price: str
    stock_status: str
    permalink: str


class ProductPage(BaseModel):
    products: list[Product]
    page: int
    total: int
    total_pages: int


class OrderSummary(BaseModel):
    id: int
    number: str
    status: str
    total: str
    currency: str
    created_at: str
    customer: str
    item_count: int


class OrderPage(BaseModel):
    orders: list[OrderSummary]
    page: int
    total: int
    total_pages: int
    window: str


class LineItem(BaseModel):
    product_id: int
    name: str
    quantity: int
    total: str


class OrderDetail(BaseModel):
    id: int
    number: str
    status: str
    currency: str
    total: str
    shipping_total: str
    payment_method: str
    created_at: str
    customer: str
    billing_city: str
    billing_country: str
    customer_note: str
    line_items: list[LineItem]


class StatusChange(BaseModel):
    id: int
    previous_status: str
    new_status: str
    note_added: bool

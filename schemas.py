# schemas.py

from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime, date
from typing import List, Optional, Literal  # <-- AICI ESTE CORECȚIA: Am adăugat Literal

# =================================================================
# NOU: Schema pentru rezultatul validării adresei
# =================================================================
class ValidationResult(BaseModel):
    is_valid: bool
    errors: List[str] = []
    suggestions: List[str] = []

# =================================================================
# Scheme actualizate la Pydantic V2 (folosind model_config)
# =================================================================

class StoreBase(BaseModel):
    id: int
    name: str
    domain: str
    
    # Sintaxa Pydantic V2
    model_config = ConfigDict(from_attributes=True)

class ShipmentBase(BaseModel):
    id: int
    awb: Optional[str] = None
    courier: Optional[str] = None
    last_status: Optional[str] = None
    fulfillment_created_at: Optional[datetime] = None

    # Sintaxa Pydantic V2
    model_config = ConfigDict(from_attributes=True)

class OrderRead(BaseModel):
    # Câmpuri din baza de date
    id: int
    name: str
    customer: str
    created_at: datetime
    total_price: Optional[float] = None
    mapped_payment: Optional[str] = None
    shopify_status: Optional[str] = None
    derived_status: Optional[str] = None
    processing_status: str
    assigned_courier: Optional[str] = None
    store: StoreBase
    shipments: List[ShipmentBase] = []

    # Câmpuri adăugate dinamic
    latest_shipment: Optional[ShipmentBase] = Field(default=None)
    line_items_str: Optional[str] = Field(default=None)
    mapped_courier_status: Optional[str] = Field(default=None)

    # Sintaxa Pydantic V2
    model_config = ConfigDict(from_attributes=True)

class OrderBase(BaseModel):
    # Aici pui câmpurile care sunt comune la creare și citire
    # De exemplu, cele pe care le primești de la Shopify
    name: str
    shopify_order_id: int
    store_id: int
    customer: Optional[str] = None
    total_price: Optional[float] = None
    # Adaugă orice alte câmpuri primești de la Shopify și vrei să le salvezi

class OrderCreate(OrderBase):
    # Aceasta este schema folosită la crearea unei comenzi.
    # Nu are nevoie de câmpuri suplimentare în acest moment,
    # dar o avem separată pentru o bună structură.
    pass

# Poți adăuga și schema pentru citire, care extinde OrderBase
# și include câmpurile generate de baza de date (ex: id)
class Order(OrderBase):
    id: int
    created_at: datetime
    processing_status: str

    model_config = ConfigDict(from_attributes=True)

class SyncPayload(BaseModel):
    store_ids: list[int]
    days: int = 30

# === SCHEME NOI PENTRU INTERFAȚA DE CREARE AWB ===

class DpdServiceOption(BaseModel):
    id: int
    name: str

class AwbCreateOptions(BaseModel):
    service_id: Optional[int] = None
    parcels_count: int = 1
    total_weight: float = 1.0
    cod_amount: Optional[float] = None
    payer: Literal['SENDER', 'RECIPIENT'] = 'SENDER'
    pickup_date: Optional[date] = None

class AwbCreateParams(BaseModel):
    order_id: int
    courier_account_key: str
    options: AwbCreateOptions
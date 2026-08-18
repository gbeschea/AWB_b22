# /models.py

from datetime import datetime
from sqlalchemy import Column, Integer, String, ForeignKey, Text, Float, Table, Index, Boolean
from sqlalchemy.dialects.postgresql import TIMESTAMP, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON
from sqlalchemy.sql import func
from database import Base
from encrypted_types import EncryptedString, EncryptedJSON
from settings import settings
import sqlalchemy as sa


# --- Hărți pentru relații Many-to-Many ---
courier_category_map = Table('courier_category_map', Base.metadata,
    Column('category_id', Integer, ForeignKey('courier_categories.id'), primary_key=True),
    Column('courier_key', String(64), primary_key=True)
)
store_category_map = Table('store_category_map', Base.metadata,
    Column('category_id', Integer, ForeignKey('store_categories.id'), primary_key=True),
    Column('store_id', Integer, ForeignKey('stores.id'), primary_key=True)
)

# --- Modele de Bază ---
class CourierCategory(Base):
    __tablename__ = 'courier_categories'
    id = Column(Integer, primary_key=True)
    name = Column(String(255), unique=True, nullable=False)
    tracking_url_template = Column(String(512), nullable=True)

class StoreCategory(Base):
    __tablename__ = 'store_categories'
    id = Column(Integer, primary_key=True)
    name = Column(String(255), unique=True, nullable=False)
    stores = relationship("Store", secondary=store_category_map, back_populates="categories")
    default_courier = Column(String(64), nullable=True)
    default_courier_account = Column(String(64), nullable=True)
    dpd_pickup_location_id = Column(String(255), nullable=True)

class Organization(Base):
    """A group of stores operated together in one dashboard. Stores join by redeeming the
    org's link_code from inside their own embedded app (proving control of both admins), so
    linking is authorized — never automatic across unrelated merchants."""
    __tablename__ = 'organizations'
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=True)
    link_code = Column(String(64), unique=True, nullable=False, index=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    stores = relationship('Store', back_populates='organization')


class Store(Base):
  __tablename__ = 'stores'
  id = Column(Integer, primary_key=True)
  name = Column(String(255))
  domain = Column(String(255), unique=True)
  # Encrypted at rest (AES-256-GCM). Legacy plaintext is read transparently. See encrypted_types.py.
  shared_secret = Column(EncryptedString, nullable=True)
  access_token = Column(EncryptedString, nullable=True)
  # Offline tokens now EXPIRE (Shopify rejects non-expiring ones for public apps). NULL expiry means
  # a legacy permanent token, re-minted by token exchange on the next request. The refresh token is
  # what lets background work (webhooks, the AWB cron) renew without a merchant present; it lives
  # 90 days and is single-use — each refresh returns a new one.
  token_expires_at = Column(TIMESTAMP(timezone=True), nullable=True)
  refresh_token = Column(EncryptedString, nullable=True)
  refresh_token_expires_at = Column(TIMESTAMP(timezone=True), nullable=True)
  api_version = Column(String(32), default=settings.SHOPIFY_API_VERSION, nullable=False)
  pii_source = Column(String(32), default='shopify', nullable=False)
  is_active = Column(Boolean, default=True, nullable=False)
  last_sync_at = Column(TIMESTAMP(timezone=True), nullable=True)
  # Billing (cached from Shopify activeSubscriptions; Shopify is source of truth).
  plan = Column(String(32), default='free', nullable=False)
  subscription_gid = Column(String(255), nullable=True)
  subscription_status = Column(String(32), nullable=True)
  # COMP: our OWN stores get the Pro entitlement (unlimited labels) WITHOUT a Shopify subscription — it's our
  # app on our own shops, so paying Shopify's revenue-share on circular money makes no sense. entitled_plan_key()
  # honours this; EXTERNAL installs default False (they subscribe or stay Free). Set explicitly, never by sync.
  comp = Column(Boolean, default=False, nullable=False)
  orders = relationship('Order', back_populates='store')
  categories = relationship("StoreCategory", secondary=store_category_map, back_populates="stores")
  organization = relationship("Organization", back_populates="stores")
  paper_size = Column(String(16), default='A6', nullable=False)
  dpd_client_id = Column(String(255), nullable=True)
  # Sender/expeditor name printed on the AWB (overrides the courier account's default). Per
  # store, so an operator running several shops can ship each under its own brand name.
  sender_name = Column(String(255), nullable=True)
  # AWB content / shipping-description template. Placeholders: ${orderName} ${quantity} ${sku}.
  # Merchant-overridable; NULL falls back to the default "${orderName} / ${quantity} x ${sku}".
  content_template = Column(String(255), nullable=True)
  # ── Parcel packing (managed IN-APP) ────────────────────────────────────────────────────────
  # How many UNITS of a product fit in one parcel, so the app splits big orders into N parcels.
  # Per-product values live in `packing_rules` (by SKU); this is the fallback for un-listed SKUs.
  default_pieces_per_parcel = Column(Float, nullable=True)
  # Mixed-order rounding: 'shared' = ceil(Σ qty/ppp) (products share boxes); 'per_product' =
  # Σ ceil(qty/ppp) (each product its own box). Per-order override wins; the tag below also forces it.
  packing_rounding = Column(String(16), default='shared', nullable=False)
  # Fallback box type for products with no specific box (its dims go on the AWB, sent to the courier).
  default_box_id = Column(Integer, ForeignKey('packing_boxes.id', ondelete='SET NULL'), nullable=True)
  # OPTIONAL/advanced import path: read the density from a Shopify product/variant metafield
  # ("namespace.key", value = parcels PER PIECE) instead of the in-app table.
  packing_metafield = Column(String(255), nullable=True)
  # OPTIONAL/advanced: an order carrying THIS tag is rounded per-product regardless of the mode.
  packing_per_product_tag = Column(String(128), nullable=True)
  # Automated AWB creation (opt-in). When on, a background job creates AWBs for eligible orders
  # (valid address + resolvable courier + not yet shipped/cancelled), but ONLY inside the window
  # below. NULL window hours = all day. Hours are 0-23 in Europe/Bucharest local time.
  auto_awb_enabled = Column(Boolean, default=False, nullable=False)
  awb_window_start = Column(Integer, nullable=True)   # ALLOWED window, whole hours (0-23), wraps past midnight
  awb_window_end = Column(Integer, nullable=True)
  # BLACKOUT interval (minute precision, minutes-of-day 0-1439): NO auto-AWB inside [start,end), wraps past
  # midnight when end<start. Independent of the allowed window — use for a lunch pause, a nightly freeze, etc.
  awb_blackout_start = Column(Integer, nullable=True)
  awb_blackout_end = Column(Integer, nullable=True)
  # Which courier account the automation uses. Auto-AWB does NOTHING until this (or a profile) is
  # set — we never guess a courier for an auto-dispatched shipment.
  auto_awb_account_key = Column(String(64), nullable=True)
  # Preferred: a saved ShipmentProfile drives auto-AWB, so automatic labels use the SAME preset
  # (courier + parcels + weight + dims + content) as manual ones. Overrides auto_awb_account_key.
  auto_awb_profile_id = Column(Integer, ForeignKey('shipment_profiles.id', ondelete='SET NULL'), nullable=True)
  # Buffer before an order is auto-dispatched: only make the AWB once the order is at least this
  # many minutes old (a cancellation / address-fix / COD-confirmation window). NULL/0 = immediate.
  auto_awb_delay_minutes = Column(Integer, nullable=True)
  # ── Test mode ─────────────────────────────────────────────────────────────────────────────
  # A sandbox the merchant (or an App Store reviewer) turns on to exercise every feature with fake
  # couriers/invoices/orders before any real credentials exist. Everything seeded is marked
  # `is_demo` so leaving test mode removes exactly what test mode created and nothing else.
  test_mode = Column(Boolean, default=False, nullable=False)
  # Set the first time the merchant LEAVES test mode. After that the app stops offering it on the
  # home page — they've seen it and decided; re-entry lives in Settings only, so the onboarding
  # nudge can't become a recurring ad for a feature they already tried.
  test_mode_used = Column(Boolean, default=False, nullable=False)
  is_demo = Column(Boolean, default=False, nullable=False, index=True)
  # Multi-store dashboard: stores whose app-install shares an Organization can be operated
  # together (view one / a group / all). `store_group` is a free-text group label within the org.
  organization_id = Column(Integer, ForeignKey('organizations.id'), nullable=True, index=True)
  store_group = Column(String(128), nullable=True, index=True)
  # --- Lifecycle automation settings (courier-status → Shopify sync + refusal handling) ---
  # Poll couriers and push fulfillment/tracking/delivery events back into Shopify.
  status_sync_enabled = Column(Boolean, default=True, nullable=False)
  # Email the customer when we create the fulfillment + tracking (Shopify "shipped" notice).
  fulfill_notify_customer = Column(Boolean, default=False, nullable=False)
  # Auto-cancel an order when the courier reports refused/returned — COD only, NEVER paid.
  auto_cancel_on_refusal = Column(Boolean, default=False, nullable=False)
  # On that auto-cancel: email the customer / restock the items.
  refusal_notify_customer = Column(Boolean, default=False, nullable=False)
  refusal_restock = Column(Boolean, default=True, nullable=False)
  # --- Customer-Service backlog / call-queue config (all in one JSONB blob) ---
  # Keys: auto_hold(bool) auto_enqueue_wrong_address(bool) value_min(float)
  #       products_any([sku]) tags_any([tag]) categories_any([name])
  #       duplicate_enabled(bool) duplicate_window_hours(int) duplicate_match('phone'|'email'|'address')
  #       flag_tag(str, added to Shopify when enqueued) hold_reason(str)
  cs_settings = Column(JSONB, nullable=True)
  invoice_settings = Column(JSONB, nullable=True)  # SmartBill per-store invoice config
  # --- Warehouse scanning (the /app/scan PWA) ---
  # Require scanning EACH item (item-level verification) before a pick/pack stage can be completed.
  # Off = order-level only (scan the order, confirm the stage). On = scan every unit.
  scan_require_item = Column(Boolean, default=False, nullable=False)
  # Allow the scanner's stock mode to WRITE inventory to Shopify (needs write_inventory).
  scan_to_stock_enabled = Column(Boolean, default=False, nullable=False)

class Order(Base):
  __tablename__ = 'orders'
  id = Column(Integer, primary_key=True)
  store_id = Column(Integer, ForeignKey('stores.id'))
  shopify_order_id = Column(String(50), unique=True, index=True)
  name = Column(String(64))               # the ORDER name/number (e.g. GT1234) — not PII
  customer = Column(EncryptedString)      # recipient display name — PII, encrypted at rest
  created_at = Column(TIMESTAMP(timezone=True))
  updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())
  cancelled_at = Column(TIMESTAMP(timezone=True), nullable=True)
  financial_status = Column(String(64), index=True)
  total_price = Column(Float, nullable=True)
  payment_gateway_names = Column(Text, nullable=True)
  mapped_payment = Column(String(64), nullable=True)
  tags = Column(Text, nullable=True)
  note = Column(Text, nullable=True)
  # Parcel count for AWB creation — source of truth is Order Hub. Synced from the Shopify parcel-count
  # metafield at ingest ("preia"), editable in OH, remembered here ("memorează"), and written back to the
  # metafield when set in OH. NULL = fall back to the shipment profile's default_parcels.
  parcel_count = Column(Integer, nullable=True)
  sync_status = Column(String(32), default='not_synced')
  last_sync_at = Column(TIMESTAMP(timezone=True), nullable=True)
  shopify_status = Column(String(64), nullable=True, index=True)
  fulfilled_at = Column(TIMESTAMP(timezone=True), nullable=True)
  # Recipient PII is encrypted at rest (AES-256-GCM) to meet Shopify's Level-2 protected-customer-
  # data requirement. Because encrypted columns can't be searched with LIKE/=, the two fields CS
  # actually looks orders up by — phone and email — also carry a deterministic blind index
  # (see the mapper event below), and locker detection is precomputed into `is_locker`. Coarse
  # geography (city/province/country) is not personally identifying on its own and stays plaintext
  # so region filters keep working.
  shipping_name = Column(EncryptedString, nullable=True)
  # Recipient email. Filled at ingest; when the customer left none we synthesize a placeholder
  # ("<store-handle>-<order#>@<shop-domain>") so couriers that REQUIRE an email (e.g. Packeta)
  # don't reject the AWB. It is a placeholder for courier validation — not a real inbox.
  shipping_email = Column(EncryptedString, nullable=True)
  shipping_email_bidx = Column(String(64), nullable=True, index=True)
  shipping_address1 = Column(EncryptedString, nullable=True)
  shipping_address2 = Column(EncryptedString, nullable=True)
  shipping_phone = Column(EncryptedString, nullable=True)
  shipping_phone_bidx = Column(String(64), nullable=True, index=True)
  is_locker = Column(Boolean, nullable=True, index=True)  # precomputed from the address at ingest
  shipping_city = Column(String(255), nullable=True)
  shipping_zip = Column(EncryptedString, nullable=True)
  shipping_province = Column(String(255), nullable=True)
  shipping_country = Column(String(255), nullable=True)
  address_status = Column(String(32), default='nevalidat', index=True, nullable=False)
  address_score = Column(Integer, nullable=True)
  address_validation_errors = Column(JSON, nullable=True)
  processing_status = Column(String(32), default='pending_validation', index=True, nullable=False)
  assigned_courier = Column(String(64), nullable=True)
  is_on_hold_shopify = Column(Boolean, default=False, nullable=False, index=True)
  derived_status = Column(String(255), nullable=True, index=True)
  store = relationship('Store', back_populates='orders')
  line_items = relationship('LineItem', back_populates='order', cascade='all, delete-orphan')
  shipments = relationship('Shipment', back_populates='order', cascade='all, delete-orphan')
  fulfillment_orders = relationship('FulfillmentOrder', back_populates='order', cascade='all, delete-orphan')
  address_validations = relationship("AddressValidation", back_populates="order", cascade="all, delete-orphan")
  assigned_profile_id = Column(Integer, ForeignKey('shipment_profiles.id'), nullable=True)
  assigned_profile = relationship("ShipmentProfile")
  # SmartBill fiscal invoice (RO)
  invoice_series = Column(String(32), nullable=True)
  invoice_number = Column(String(32), nullable=True)
  invoice_url = Column(Text, nullable=True)
  invoiced_at = Column(TIMESTAMP(timezone=True), nullable=True)
  # Warehouse pick/pack workflow (the /app/scan PWA): new → picked → packed → dispatched.
  # Seeded by Test mode — deleted when test mode is switched off.
  is_demo = Column(Boolean, default=False, nullable=False, index=True)
  pick_stage = Column(String(16), default='new', nullable=False, index=True)
  # In-progress item verification for the CURRENT stage: {sku: units_scanned}. Reset on advance.
  pick_progress = Column(JSONB, nullable=True)


# Locker/pickup-point keywords — an order whose address mentions one is a locker delivery.
# Kept here (not just in routes/api.py) because `is_locker` is computed at write time.
_LOCKER_KEYWORDS = (
    "easybox", "locker", "pachetomat", "pick-up", "pickup", "packeta",
    "ship & go", "ship&go", "fanbox", "pudo", "paczkomat", "box now",
)


def _looks_like_locker(*parts) -> bool:
    blob = " ".join(p for p in parts if p).lower()
    return any(kw in blob for kw in _LOCKER_KEYWORDS)


@sa.event.listens_for(Order, "before_insert")
@sa.event.listens_for(Order, "before_update")
def _order_sync_pii_derived(mapper, connection, target: "Order") -> None:
    """Keep the searchable derivations of the encrypted PII in sync on every write.

    Runs before the column type encrypts the values, so `target.shipping_phone` etc. are still
    plaintext here. Centralising it means no ingest path (webhook, backfill, manual edit, the
    migration) can forget to populate the blind indexes or the locker flag.
    """
    import crypto
    target.shipping_phone_bidx = crypto.phone_blind_index(target.shipping_phone)
    target.shipping_email_bidx = crypto.blind_index(target.shipping_email)
    target.is_locker = _looks_like_locker(
        getattr(target, "shipping_address1", None), getattr(target, "shipping_address2", None)
    )


class Shipment(Base):
  __tablename__ = 'shipments'
  id = Column(Integer, primary_key=True)
  order_id = Column(Integer, ForeignKey('orders.id'))
  fulfillment_created_at = Column(TIMESTAMP(timezone=True), nullable=True)
  shopify_fulfillment_id = Column(String(50), unique=True, index=True, nullable=True)
  awb = Column(String(64), index=True)
  courier_specific_data = Column(JSON, nullable=True)
  courier = Column(String(64), index=True)
  account_key = Column(String(32))
  paper_size = Column(String(16))
  printed_at = Column(TIMESTAMP(timezone=True), nullable=True, index=True)
  last_status = Column(String(255), nullable=True, index=True)
  last_status_at = Column(TIMESTAMP(timezone=True), nullable=True)
  derived_status = Column(String(255), nullable=True)
  # When THIS label was created by the app — the basis for the monthly plan quota. Indexed so the
  # per-store month count is cheap. Historical rows (pre-migration) are NULL and don't count.
  created_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=True, index=True)
  order = relationship('Order', back_populates='shipments')

class RomaniaAddress(Base):
    __tablename__ = 'romania_addresses'
    id = Column(Integer, primary_key=True)
    judet = Column(String(255), index=True)
    localitate = Column(String(255), index=True)
    sector = Column(String(32), nullable=True, index=True)
    tip_artera = Column(String(64), nullable=True, index=True)
    nume_strada = Column(String(512), nullable=True, index=True)
    cod_postal = Column(String(10), index=True)
    __table_args__ = (Index('ix_localitate_judet', 'localitate', 'judet'),)

class LineItem(Base):
  __tablename__ = 'line_items'
  id = Column(Integer, primary_key=True)
  order_id = Column(Integer, ForeignKey('orders.id'))
  sku = Column(String(128), index=True)
  title = Column(Text)
  quantity = Column(Integer)
  # Shopify product tags, lowercased + pipe-wrapped for exact-token filtering: |fragile|gift wrap|
  # ("|" alone = fetched but the product has no tags; NULL = not fetched yet).
  product_tags = Column(Text, nullable=True)
  order = relationship('Order', back_populates='line_items')

class FulfillmentOrder(Base):
    __tablename__ = 'fulfillment_orders'
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey('orders.id'), index=True)
    shopify_fulfillment_order_id = Column(String(50), unique=True, index=True)
    status = Column(String(64))
    hold_details = Column(JSON, nullable=True)
    order = relationship('Order', back_populates='fulfillment_orders')

class PrintLog(Base):
    __tablename__ = 'print_logs'
    id = Column(Integer, primary_key=True)
    # TENANCY: print history is per-shop. Without this the /api/print/logs endpoint returned every
    # merchant's batches — order names, AWBs and per-SKU shipped volumes — to every other merchant.
    # Nullable because rows created before this column existed have no owner; those are treated as
    # legacy and shown to nobody.
    store_id = Column(Integer, ForeignKey('stores.id'), index=True, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    category_name = Column(String(255))
    category_id = Column(Integer)
    awb_count = Column(Integer)
    user_ip = Column(String(45), nullable=True)
    pdf_path = Column(String(512), nullable=True)
    entries = relationship('PrintLogEntry', back_populates='log', cascade='all, delete-orphan')

class PrintLogEntry(Base):
    __tablename__ = 'print_log_entries'
    id = Column(Integer, primary_key=True)
    print_log_id = Column(Integer, ForeignKey('print_logs.id'))
    order_name = Column(String(64))
    awb = Column(String(64), index=True)
    log = relationship('PrintLog', back_populates='entries')


class PrintBatch(Base):
    """A prepared print job — a named set of orders/AWBs staged (by a rule, the in-app command
    box, or later Sidekick) for a warehouse operator to grab and print. Decouples 'decide what to
    print' from 'actually print'. `awbs` is a snapshot at prepare time; printing re-filters to the
    still-unprinted ones and re-counts."""
    __tablename__ = 'print_batches'
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    criteria = Column(JSONB, nullable=True)          # the filter used to build it (audit / re-run)
    awbs = Column(JSONB, nullable=False, default=list)  # snapshot AWB list
    order_count = Column(Integer, default=0)
    status = Column(String(16), default='pending', index=True)  # pending | printed | cancelled
    source = Column(String(32), default='manual')    # manual | command | rule | sidekick
    created_by = Column(String(128), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, index=True)
    printed_at = Column(TIMESTAMP(timezone=True), nullable=True)
    print_log_id = Column(Integer, ForeignKey('print_logs.id'), nullable=True)


class PickingList(Base):
    """A named, saved picking run — the warehouse equivalent of a print batch.

    The Picking page used to be a LIVE view only, so a picker had no way to say "this is the batch
    I'm walking the aisles with now" or to come back to it. A list snapshots the order ids it was
    created from, so what the picker is holding can't shift under them when new orders sync in."""
    __tablename__ = 'picking_lists'
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    lens = Column(String(32), nullable=True)          # awb | unfulfilled | all — the batch it came from
    scope = Column(String(64), nullable=True)         # store scope it was built for
    order_ids = Column(JSONB, nullable=False, default=list)   # snapshot at creation
    total_orders = Column(Integer, default=0)
    total_units = Column(Integer, default=0)
    total_skus = Column(Integer, default=0)
    status = Column(String(16), default='open', index=True)   # open | picked | cancelled
    created_by = Column(String(128), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, index=True)
    completed_at = Column(TIMESTAMP(timezone=True), nullable=True)


class PrintRule(Base):
    """A reusable, named print filter — re-run any time to prepare a fresh batch (e.g. 'daily HA-1303
    singles'). Same criteria shape as PrintBatch.criteria."""
    __tablename__ = 'print_rules'
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    criteria = Column(JSONB, nullable=False, default=dict)
    enabled = Column(Boolean, default=True, nullable=False)
    created_by = Column(String(128), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)


class ScanEvent(Base):
    """Audit trail of every warehouse scan (the /app/scan PWA): a stage advance, an item
    verification, or a stock adjustment. Feeds the recent-scans log and offline reconciliation."""
    __tablename__ = 'scan_events'
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=True, index=True)
    order_id = Column(Integer, ForeignKey('orders.id', ondelete='SET NULL'), nullable=True, index=True)
    stage = Column(String(16), nullable=True)     # picked | packed | dispatched (for stage events)
    kind = Column(String(16), nullable=False)     # order | item | stage | stock
    code = Column(String(128), nullable=True)     # the raw scanned string
    sku = Column(String(128), nullable=True)
    quantity = Column(Integer, nullable=True)
    ok = Column(Boolean, default=True, nullable=False)
    note = Column(Text, nullable=True)
    created_by = Column(String(128), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, index=True)

class CourierAccount(Base):
    __tablename__ = 'courier_accounts'
    id = Column(Integer, primary_key=True)
    # Multi-tenancy: which shop owns this account. NULL = legacy/shared (pre-migration).
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    account_key = Column(String(64), unique=True, nullable=False, index=True)
    courier_type = Column(String(64), nullable=False, index=True)
    tracking_url = Column(String(512), nullable=True)
    # Encrypted at rest (AES-256-GCM). Legacy plaintext JSONB is read transparently. See encrypted_types.py.
    credentials = Column(EncryptedJSON, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    # Seeded by Test mode (the sandbox courier) — deleted when test mode is switched off.
    is_demo = Column(Boolean, default=False, nullable=False, index=True)
    mappings = relationship("CourierMapping", back_populates="account")

class CourierMapping(Base):
    __tablename__ = 'courier_mappings'
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=True, index=True)
    shopify_name = Column(String(255), unique=True, nullable=False, index=True)
    account_key = Column(String(64), ForeignKey('courier_accounts.account_key'), nullable=False)
    account = relationship("CourierAccount", back_populates="mappings")

class AddressValidation(Base):
    __tablename__ = 'address_validations'
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=False, index=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    validator_version = Column(String(32))
    status = Column(String(64), index=True)
    score = Column(Integer)
    errors = Column(JSONB)
    suggestions = Column(JSONB)
    
    order = relationship("Order", back_populates="address_validations")

class ValidationPolicy(Base):
    # Politicile ALEGIBILE ale validării (toggle-urile de business), separate de regulile de corectitudine
    # (cod). Moștenire pe 3 niveluri (services/settings/resolver.py): store_id&organization_id NULL = GLOBAL;
    # organization_id set + store_id NULL = nivel ORGANIZAȚIE; store_id set = override MAGAZIN. `policies` =
    # JSONB cu cheile din services/nomenclator/policy.py::POLICY_DEFAULTS (cheile lipsă cad pe nivelul de sus).
    __tablename__ = 'validation_policy'
    # oglindește indexul unic parțial din awb_parity_migrate.sql, ca alembic autogenerate să NU-l șteargă
    __table_args__ = (
        sa.Index('uq_validation_policy_org', 'organization_id', unique=True,
                 postgresql_where=sa.text('store_id IS NULL AND organization_id IS NOT NULL')),
    )
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=True, unique=True, index=True)
    organization_id = Column(Integer, ForeignKey('organizations.id'), nullable=True, index=True)
    policies = Column(JSONB, nullable=False, server_default='{}')
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())


class HubSettings(Base):
    # Setările de CAPABILITATE (duplicates, blocklist, …) ca blob JSONB, moștenite org→magazin (resolver.py).
    # organization_id set + store_id NULL = nivel ORGANIZAȚIE (toate magazinele moștenesc); store_id set =
    # override MAGAZIN. settings = { "<capabilitate>": {"preset": "...", <knob brute Avansat>} }.
    __tablename__ = 'hub_settings'
    __table_args__ = (
        sa.Index('uq_hub_settings_org', 'organization_id', unique=True,
                 postgresql_where=sa.text('store_id IS NULL AND organization_id IS NOT NULL')),
    )
    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey('organizations.id'), nullable=True, index=True)
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=True, unique=True, index=True)
    settings = Column(JSONB, nullable=False, server_default='{}')
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())


class BlocklistEntry(Base):
    # Clienți BLOCAȚI manual (îi punem noi din UI/CS) — comanda lor NU primește auto-AWB (cronul o anulează).
    # Serial-refuser NU se stochează aici; se calculează la rulare din istoricul de shipments (blocklist.py).
    # Matching pe BLIND-INDEX (fără decriptare PII). store_id NULL = intrare globală (toate magazinele).
    __tablename__ = 'blocklist'
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=True, index=True)
    match_type = Column(String(16), nullable=False)                        # phone | email
    value_bidx = Column(String(64), nullable=False, index=True)
    reason = Column(Text, nullable=True)
    source = Column(String(16), nullable=False, server_default='manual')   # manual | serial_refuser
    active = Column(Boolean, nullable=False, server_default=sa.text('true'))
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)

class ShipmentProfile(Base):
    __tablename__ = 'shipment_profiles'
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=True, index=True)
    name = Column(String(255), unique=True, nullable=False)
    account_key = Column(String(64), ForeignKey('courier_accounts.account_key'), nullable=False)
    default_parcels = Column(Integer, default=1)
    default_weight_kg = Column(Float, default=1.0)
    default_width_cm = Column(Integer, nullable=True)
    default_height_cm = Column(Integer, nullable=True)
    default_length_cm = Column(Integer, nullable=True)
    content_template = Column(String(255), default='${orderName} / ${quantity} x ${sku}')
    default_service_id = Column(Integer, nullable=True) # Asigură-te că ai și acest câmp
    default_payer = Column(String(32), default='SENDER')
    default_label_size = Column(String(8), nullable=True)  # A6 | A4 — auto-set when the profile is picked

    # Stocăm un șablon al payload-ului DPD
    # Aici pot fi salvate setările refolosibile
    dpd_payload_template = Column(JSONB, nullable=True)
    default_packing = sa.Column(sa.String(20), nullable=True)  # DPD: BOX | PALLET | ENVELOPE | BAG | WRAP

    account = relationship("CourierAccount")


class ShipmentRule(Base):
    """A conditional routing rule for automation: IF the order matches `conditions` THEN ship it
    with `profile` (which carries courier + parcels + weight + content). Rules are evaluated in
    `priority` order (low first); the FIRST enabled match wins. Empty conditions = catch-all."""
    __tablename__ = 'shipment_rules'
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    priority = Column(Integer, default=100, nullable=False)  # lower = evaluated first
    enabled = Column(Boolean, default=True, nullable=False)
    # Structured conditions, ALL present keys must pass (AND); values within a key are OR.
    # Keys: tags_any, sku_any, content_contains, county_any, city_contains, country_any,
    #       total_min, total_max.
    conditions = Column(JSONB, nullable=False, default=dict)
    profile_id = Column(Integer, ForeignKey('shipment_profiles.id', ondelete='CASCADE'), nullable=False)

    profile = relationship("ShipmentProfile")


class PackingBox(Base):
    """A reusable box/envelope type. Its dimensions go on the AWB (sent to the courier)."""
    __tablename__ = 'packing_boxes'
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    box_type = Column(String(16), default='BOX', nullable=False)  # BOX | ENVELOPE
    length_cm = Column(Integer, nullable=True)
    width_cm = Column(Integer, nullable=True)
    height_cm = Column(Integer, nullable=True)


class PackingRule(Base):
    """Per-product packing set IN-APP (matched by SKU): how many pieces fit a parcel, which box,
    and the unit weight — applied AUTOMATICALLY on every order (no manual per-shipment picking)."""
    __tablename__ = 'packing_rules'
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=True, index=True)
    sku = Column(String(128), nullable=False, index=True)
    title = Column(String(255), nullable=True)       # cached product title for display
    image_url = Column(Text, nullable=True)          # cached product image for display
    pieces_per_parcel = Column(Float, nullable=True) # units that fit one box (10 ⇒ 11 units = 2 parcels)
    weight_kg = Column(Float, nullable=True)         # unit weight; mirrors Shopify, summed onto the AWB
    box_id = Column(Integer, ForeignKey('packing_boxes.id', ondelete='SET NULL'), nullable=True)
    # Warehouse pick location — where the picker walks to. Shown + sorted on the picking list.
    location = Column(String(64), nullable=True)         # zone / area / room
    shelf = Column(String(64), nullable=True)            # raft
    shelf_position = Column(String(64), nullable=True)   # poziție pe raft

    box = relationship("PackingBox")
    __table_args__ = (Index('ux_packing_rule_store_sku', 'store_id', 'sku', unique=True),)


class CSQueueItem(Base):
    """One entry in the Customer-Service backlog / call-queue. An order lands here (auto or
    manually) because it needs a human before it can ship: bad address, matched a CS rule,
    looks like a duplicate, contains an out-of-stock product, or was pushed in by hand.
    The order is (optionally) put ON HOLD in Shopify while it sits here; resolving releases it."""
    __tablename__ = 'cs_queue_items'
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=True, index=True)
    # One queue entry per order (re-enqueue updates the same row).
    order_id = Column(Integer, ForeignKey('orders.id', ondelete='CASCADE'), nullable=False, unique=True, index=True)
    reason = Column(String(32), nullable=False, index=True)  # wrong_address|rule|duplicate|out_of_stock|manual
    reason_detail = Column(Text, nullable=True)              # rule name / OOS sku / free note
    status = Column(String(16), default='open', nullable=False, index=True)  # open|in_progress|solved
    priority = Column(Integer, nullable=True)               # optional manual bump (higher = more urgent)
    notes = Column(JSONB, nullable=True)                    # running CS log: [{at, text, by}]
    was_held = Column(Boolean, default=False, nullable=False)  # did WE put it on hold on enqueue
    created_by = Column(String(16), default='auto', nullable=False)  # auto|manual
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    solved_at = Column(TIMESTAMP(timezone=True), nullable=True)

    order = relationship("Order")


class CSEmailTemplate(Base):
    """A merchant-defined email the CS agent can send to a customer from an order (via Shopify's
    order-invoice email channel). Placeholders in subject/body: {customer} {order} {address}
    {store} {phone} {tracking}. `auto_on` (an enqueue reason) fires it automatically."""
    __tablename__ = 'cs_email_templates'
    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey('stores.id'), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    subject = Column(String(500), nullable=False)
    body = Column(Text, nullable=False)
    # Shown to the agent in the send dialog: WHEN this template is the right one to send. Without
    # it a new agent sees a list of names and has to guess.
    description = Column(String(500), nullable=True)
    # NULL = manual-only. Else the enqueue reason (e.g. 'wrong_address') that auto-sends this once.
    auto_on = Column(String(32), nullable=True, index=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)


class AppLedger(Base):
    """Reinstall-proof per-shop flags (e.g. free trial already used). Keyed by a salted
    hash of the shop domain and deliberately has NO FK to Store, so it survives an
    uninstall + shop/redact and the trial cannot be reset by reinstalling."""
    __tablename__ = 'app_ledger'
    id = Column(Integer, primary_key=True)
    domain_hash = Column(String(64), unique=True, nullable=False, index=True)
    trial_used = Column(Boolean, default=False, nullable=False)
    first_seen = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)

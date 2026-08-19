/**
 * Authenticated fetch for the Order Hub backend.
 *
 * Every `/api/*` call must carry the App Bridge session token as
 * `Authorization: Bearer <idToken>`. The backend verifies it (HS256 JWT signed
 * with the app secret) and resolves the shop. `shopify.idToken()` returns a fresh,
 * short-lived token on each call, so we fetch it per request.
 */

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export async function authFetch<T = unknown>(
  path: string,
  opts: RequestInit = {},
): Promise<T> {
  const token = await shopify.idToken();
  const res = await fetch(path, {
    ...opts,
    headers: {
      ...(opts.headers ?? {}),
      Authorization: `Bearer ${token}`,
    },
  });

  if (!res.ok) {
    let detail = res.statusText || `Request failed (${res.status})`;
    try {
      const body = (await res.json()) as { detail?: string; error?: string };
      detail = body.detail ?? body.error ?? detail;
    } catch {
      /* non-JSON error body — keep the status text */
    }
    throw new ApiError(res.status, detail);
  }

  return (await res.json()) as T;
}

/** Like authFetch but returns the raw response body as a Blob (for PDFs etc.). */
export async function authFetchBlob(path: string, opts: RequestInit = {}): Promise<Blob> {
  const token = await shopify.idToken();
  const res = await fetch(path, {
    ...opts,
    headers: { ...(opts.headers ?? {}), Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    let detail = res.statusText || `Request failed (${res.status})`;
    try {
      const body = (await res.json()) as { detail?: string; error?: string };
      detail = body.detail ?? body.error ?? detail;
    } catch { /* non-JSON error body */ }
    throw new ApiError(res.status, detail);
  }
  return await res.blob();
}

/** Shape of `GET /api/me`. */
export interface MeResponse {
  shop: string;
  name: string;
  is_active: boolean;
  api_version: string;
  plan: string;
  missing_scopes?: string[];
  reauth_url?: string | null;
}
export function getMe() {
  return authFetch<MeResponse>("/api/me");
}

/** One row of `GET /api/orders`. */
export interface OrderRow {
  id: number;
  in_cs: boolean;
  is_demo?: boolean;
  name: string | null;
  customer: string | null;
  created_at: string | null;
  total_price: number | null;
  financial_status: string | null;
  processing_status: string | null;
  derived_status: string | null;
  address_status: string | null;
  address_score: number | null;
  city: string | null;
  phone: string | null;
  assigned_courier: string | null;
  shipment_id: number | null;
  awb: string | null;
  courier: string | null;
  last_status: string | null;
  printed: boolean;
  invoice_number: string | null;
  invoice_url: string | null;
  financial_paid: boolean;
  on_hold: boolean;
  line_count: number;
  items: { sku: string | null; title: string | null; quantity: number | null }[];
  product_tags?: string[];
  address: {
    name: string | null; address1: string | null; address2: string | null; city: string | null;
    zip: string | null; province: string | null; country: string | null; phone: string | null;
  };
  store_domain: string | null;
  store_name: string | null;
  tracking_url: string | null;
}

export interface LensCounts {
  all: number; unfulfilled: number; fulfilled: number; in_transit: number; delivered: number; refused: number;
}
export interface OrdersResponse {
  lens_counts?: LensCounts | null;
  orders: OrderRow[];
  total: number;
  page: number;
  per_page: number;
}

export interface OrderShipmentDetail {
  id: number;
  awb: string | null;
  courier: string | null;
  account_key: string | null;
  last_status: string | null;
  last_status_at: string | null;
  derived_status: string | null;
  printed: boolean;
  location: string | null;
  tracking_url: string | null;
}
export interface OrderDetail {
  id: number;
  name: string | null;
  customer: string | null;
  created_at: string | null;
  total_price: number | null;
  financial_status: string | null;
  financial_paid: boolean;
  processing_status: string | null;
  derived_status: string | null;
  shopify_status: string | null;
  fulfilled_at: string | null;
  cancelled_at: string | null;
  tags: string | null;
  note: string | null;
  invoice_number: string | null;
  invoice_url: string | null;
  store_name: string | null;
  store_domain: string | null;
  address: {
    status: string | null; score: number | null; name: string | null; email: string | null;
    phone: string | null; address1: string | null; address2: string | null; city: string | null;
    zip: string | null; province: string | null; country: string | null;
    errors: unknown; suggestions: unknown;
  };
  line_items: { sku: string | null; title: string | null; quantity: number | null }[];
  shipments: OrderShipmentDetail[];
}

export function getOrderDetail(id: number) {
  return authFetch<OrderDetail>(`/api/orders/${id}`);
}

export interface TimelineEvent {
  id: string | null;
  message: string | null;
  created_at: string | null;
  critical: boolean;
  app: string | null;
  by_app: boolean;
  by_user: boolean;
}
export function getOrderTimeline(id: number) {
  return authFetch<{ events: TimelineEvent[] }>(`/api/orders/${id}/timeline`);
}

export interface OrderFilters {
  payment?: string; delivery?: string; printed?: string; invoiced?: string; order_status?: string;
  delivery_status?: string; address_status?: string; tag?: string; product?: string;
  product_tag?: string;
  province?: string; city?: string; courier?: string;
  qty_min?: number; qty_max?: number; total_min?: number; total_max?: number;
  date_from?: string; date_to?: string; sort?: string;
}
export function listOrders(
  params: { page?: number; per_page?: number; q?: string; status?: string; scope?: string; lens?: string;
            with_lens_counts?: boolean } & OrderFilters = {},
) {
  const qs = new URLSearchParams();
  if (params.page) qs.set("page", String(params.page));
  if (params.per_page) qs.set("per_page", String(params.per_page));
  if (params.q) qs.set("q", params.q);
  if (params.status) qs.set("status", params.status);
  if (params.scope) qs.set("scope", params.scope);
  if (params.lens && params.lens !== "all") qs.set("lens", params.lens);
  if (params.with_lens_counts) qs.set("with_lens_counts", "true");
  const keys: (keyof OrderFilters)[] = ["payment", "delivery", "printed", "invoiced", "order_status",
    "delivery_status", "address_status", "tag", "product", "product_tag", "province", "city", "courier",
    "qty_min", "qty_max", "total_min", "total_max", "date_from", "date_to", "sort"];
  for (const k of keys) {
    const v = params[k];
    if (v !== undefined && v !== "" && v !== null) qs.set(k, String(v));
  }
  const query = qs.toString();
  return authFetch<OrdersResponse>(`/api/orders${query ? `?${query}` : ""}`);
}

export function getProductTags(scope?: string) {
  const q = scope ? `?scope=${encodeURIComponent(scope)}` : "";
  return authFetch<{ tags: string[] }>(`/api/product-tags${q}`);
}

export function backfillProductTags(scope?: string) {
  const q = scope ? `?scope=${encodeURIComponent(scope)}` : "";
  return post<{ updated: number; scanned: number }>(`/api/orders/backfill-product-tags${q}`);
}

// ---- Multi-store organization ----

export interface OrgStore {
  id: number;
  domain: string;
  name: string | null;
  store_group: string | null;
  sender_name: string | null;
  is_me: boolean;
}
export interface OrgResponse {
  organization: { id: number; name: string | null } | null;
  me: OrgStore;
  stores: OrgStore[];
  groups: string[];
}

export function getOrg() {
  return authFetch<OrgResponse>("/api/org");
}
export function getLinkCode() {
  return authFetch<{ link_code: string; organization: { id: number; name: string | null } }>(
    "/api/org/link-code", { method: "POST" });
}
export function joinOrg(code: string) {
  return authFetch<{ organization: { id: number; name: string | null }; linked: number; message: string }>(
    "/api/org/join", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }),
    });
}
export function leaveOrg() {
  return authFetch<{ organization: null }>("/api/org/leave", { method: "POST" });
}
export function setStoreGroup(store_group: string) {
  return authFetch<{ id: number; store_group: string | null }>("/api/org/store", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ store_group }),
  });
}

// ---- Cross-store processing overview (the Overview home) ----
export interface OverviewStoreRow {
  id: number; domain: string; name: string; group: string | null; is_me: boolean;
  to_ship: number; address_issues: number; to_print: number;
  in_transit: number; refused: number; cs_backlog: number;
}
export type OverviewMetricKey =
  "to_ship" | "address_issues" | "to_print" | "in_transit" | "refused" | "cs_backlog";
export interface OverviewStoresResponse {
  org: { id: number; name: string | null } | null;
  multi: boolean;
  stores: OverviewStoreRow[];
  totals: Record<OverviewMetricKey, number>;
  groups: string[];
}
export function getOverviewStores() {
  return authFetch<OverviewStoresResponse>("/api/overview/stores");
}

// ---- Warehouse scanning (the /app/scan PWA) ----
export interface ScanSettings { scan_require_item: boolean; scan_to_stock_enabled: boolean; }
export interface ScanItem { sku: string | null; title: string | null; barcode: string | null; need: number; scanned: number; complete: boolean; }
export interface ScanOrderView {
  id: number; name: string | null; customer: string | null; store_name: string | null;
  pick_stage: string; cancelled: boolean; awb: string | null; courier: string | null;
  items: ScanItem[]; all_scanned: boolean; require_item: boolean;
}
export interface ScanQueueRow { id: number; name: string | null; customer: string | null; store_name: string | null; units: number; lines: number; }
export interface ScanQueue { step: string; from_stage: string; counts: Record<string, number>; orders: ScanQueueRow[]; }
export interface ScanEventRow {
  id: number; kind: string; stage: string | null; code: string | null; sku: string | null;
  quantity: number | null; ok: boolean; note: string | null; order_id: number | null; created_at: string | null;
}
export function getScanSettings() { return authFetch<ScanSettings>("/api/scan/settings"); }
export function saveScanSettings(s: Partial<ScanSettings>) {
  return authFetch<ScanSettings>("/api/scan/settings", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(s) });
}
export function scanQueue(step: string, scope?: string) {
  const qs = new URLSearchParams({ step });
  if (scope) qs.set("scope", scope);
  return authFetch<ScanQueue>(`/api/scan/queue?${qs.toString()}`);
}
export function scanLookup(code: string, scope?: string) {
  return authFetch<ScanOrderView>("/api/scan/lookup", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code, scope }) });
}
export function scanItem(orderId: number, code: string, quantity = 1) {
  return authFetch<ScanOrderView>(`/api/scan/order/${orderId}/scan-item`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code, quantity }) });
}
export function scanAdvance(orderId: number, step: string) {
  return authFetch<ScanOrderView>(`/api/scan/order/${orderId}/advance`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ step }) });
}
export function scanStock(sku: string, delta = 1) {
  return authFetch<{ success: boolean; sku: string; title: string | null; location: string | null; delta: number; new_available: number | null }>(
    "/api/scan/stock/scan", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sku, delta }) });
}
export function scanRecent(scope?: string, limit = 30) {
  const qs = new URLSearchParams({ limit: String(limit) });
  if (scope) qs.set("scope", scope);
  return authFetch<{ events: ScanEventRow[] }>(`/api/scan/recent?${qs.toString()}`);
}
/** A printable pick-sheet PDF (Code128 barcodes) for the given orders — returns the PDF blob. */
export function pickSheet(order_ids: number[]) {
  return authFetchBlob("/api/scan/pick-sheet", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ order_ids }),
  });
}

/** `GET /api/couriers` — courier config for the settings screen (never includes secrets). */
export interface CourierAccount {
  id: number;
  name: string;
  account_key: string;
  courier_type: string;
  tracking_url: string | null;
  is_active: boolean;
  has_credentials: boolean;
}
export interface CourierMapping {
  id: number;
  shopify_name: string;
  account_key: string;
}
export interface ShipmentProfile {
  id: number;
  name: string;
  account_key: string;
  default_parcels: number | null;
  default_weight_kg: number | null;
  default_length_cm: number | null;
  default_width_cm: number | null;
  default_height_cm: number | null;
  default_service_id: number | null;
  default_payer: string | null;
  default_packing: string | null;
  default_label_size: string | null;
  content_template: string | null;
}
export interface CouriersResponse {
  accounts: CourierAccount[];
  mappings: CourierMapping[];
  profiles: ShipmentProfile[];
}

export function getCouriers() {
  return authFetch<CouriersResponse>("/api/couriers");
}

/** Fields the profile create/update form sends (id/store are server-side). */
export type ShipmentProfileInput = Omit<ShipmentProfile, "id">;

export function createProfile(input: ShipmentProfileInput) {
  return authFetch<ShipmentProfile>("/api/couriers/profiles", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function updateProfile(id: number, input: ShipmentProfileInput) {
  return authFetch<ShipmentProfile>(`/api/couriers/profiles/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function deleteProfile(id: number) {
  return authFetch<{ success: boolean; deleted: number }>(`/api/couriers/profiles/${id}`, {
    method: "DELETE",
  });
}

// ---- Automation routing rules (conditions → profile) ----

export interface RuleConditions {
  tags_any?: string[];
  sku_any?: string[];
  content_contains?: string;
  county_any?: string[];
  city_contains?: string;
  country_any?: string[];
  total_min?: number;
  total_max?: number;
  items_min?: number;
  items_max?: number;
}
export interface ShipmentRule {
  id: number;
  name: string;
  priority: number;
  enabled: boolean;
  conditions: RuleConditions;
  profile_id: number;
}
export type ShipmentRuleInput = Omit<ShipmentRule, "id">;

export function listRules() {
  return authFetch<{ rules: ShipmentRule[] }>("/api/shipment-rules");
}
export function createRule(input: ShipmentRuleInput) {
  return authFetch<ShipmentRule>("/api/shipment-rules", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}
export function updateRule(id: number, input: ShipmentRuleInput) {
  return authFetch<ShipmentRule>(`/api/shipment-rules/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}
export function deleteRule(id: number) {
  return authFetch<{ success: boolean; deleted: number }>(`/api/shipment-rules/${id}`, {
    method: "DELETE",
  });
}

// ---- Parcel packing: box types + per-product rules ----

export interface PackingBox {
  id: number;
  name: string;
  box_type: string; // BOX | ENVELOPE
  length_cm: number | null;
  width_cm: number | null;
  height_cm: number | null;
}
export type PackingBoxInput = Omit<PackingBox, "id">;

export function getBoxes() {
  return authFetch<{ boxes: PackingBox[] }>("/api/packing-boxes");
}
export function createBox(input: PackingBoxInput) {
  return authFetch<PackingBox>("/api/packing-boxes", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input),
  });
}
export function updateBox(id: number, input: PackingBoxInput) {
  return authFetch<PackingBox>(`/api/packing-boxes/${id}`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input),
  });
}
export function deleteBox(id: number) {
  return authFetch<{ success: boolean; deleted: number }>(`/api/packing-boxes/${id}`, { method: "DELETE" });
}

export interface PackingRule {
  id: number;
  sku: string;
  title: string | null;
  image_url: string | null;
  pieces_per_parcel: number | null;
  weight_kg: number | null;
  box_id: number | null;
  location: string | null;
  shelf: string | null;
  shelf_position: string | null;
}
/** One rule (by SKU) or a bulk array — the merchant sets pieces/box/weight/location per product. */
export interface PackingRuleInput {
  sku: string;
  title?: string | null;
  image_url?: string | null;
  pieces_per_parcel?: number | null;
  weight_kg?: number | null;
  box_id?: number | null;
  location?: string | null;
  shelf?: string | null;
  shelf_position?: string | null;
}

export function getPackingRules() {
  return authFetch<{ rules: PackingRule[] }>("/api/packing-rules");
}
export function upsertPackingRules(rules: PackingRuleInput[]) {
  return authFetch<{ rules: PackingRule[] }>("/api/packing-rules", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rules }),
  });
}
export function deletePackingRule(id: number) {
  return authFetch<{ success: boolean; deleted: number }>(`/api/packing-rules/${id}`, { method: "DELETE" });
}

// ---- Product browser (Shopify catalog) for packing ----

export interface ProductVariant {
  id: string;
  sku: string | null;
  barcode: string | null;
  inventory_item_id: string | null;
  weight_kg: number | null;
  pieces_per_parcel: number | null;
  box_id: number | null;
  location: string | null;
  shelf: string | null;
  shelf_position: string | null;
}
export interface CatalogProduct {
  id: string;
  title: string;
  image: string | null;
  variants: ProductVariant[];
}
export function getProducts(params: { q?: string; cursor?: string } = {}) {
  const qs = new URLSearchParams();
  if (params.q) qs.set("q", params.q);
  if (params.cursor) qs.set("cursor", params.cursor);
  const query = qs.toString();
  return authFetch<{ products: CatalogProduct[]; next_cursor: string | null }>(
    `/api/products${query ? `?${query}` : ""}`,
  );
}

/** Set or GENERATE (blank barcode → EAN-13) barcodes on Shopify variants. Needs write_products. */
export function setProductBarcodes(items: { product_id: string; variant_id: string; sku?: string | null; barcode?: string }[]) {
  return authFetch<{ results: { variant_id: string; sku: string | null; barcode: string | null }[]; errors: { variant_id: string; error: string }[] }>(
    "/api/products/barcodes/set", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items }),
  });
}
export interface LabelOptions { page?: string; cols?: number; rows?: number; bar_height_mm?: number; bar_width?: number; }
/** Printable PDF of visual barcode labels (to stick on products) — configurable size/grid/page. */
export function productBarcodeLabels(
  labels: { barcode: string; sku?: string | null; title?: string | null; copies?: number }[],
  options?: LabelOptions,
) {
  return authFetchBlob("/api/products/barcode-labels", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ labels, options }),
  });
}

export interface ProductPackingItem {
  sku: string;
  inventory_item_id?: string | null;
  title?: string | null;
  image_url?: string | null;
  pieces_per_parcel?: number | null;
  weight_kg?: number | null;
  box_id?: number | null;
  location?: string | null;
  shelf?: string | null;
  shelf_position?: string | null;
}
export function saveProductPacking(items: ProductPackingItem[]) {
  return authFetch<{ saved: number; weights_written: number; errors: { sku: string; error: string }[] }>(
    "/api/products/packing",
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items }) },
  );
}

// ---- Automation settings (courier status sync + refusal auto-cancel) ----

export interface AutomationSettings {
  status_sync_enabled: boolean;
  fulfill_notify_customer: boolean;
  auto_cancel_on_refusal: boolean;
  refusal_notify_customer: boolean;
  refusal_restock: boolean;
  sender_name: string | null;
  content_template: string | null;
  packing_metafield: string | null;
  packing_per_product_tag: string | null;
  default_pieces_per_parcel: number | null;
  packing_rounding: string;
  default_box_id: number | null;
  auto_awb_enabled: boolean;
  auto_awb_account_key: string | null;
  auto_awb_profile_id: number | null;
  awb_window_start: number | null;
  awb_window_end: number | null;
  auto_awb_delay_minutes: number | null;
}

export function getAutomationSettings() {
  return authFetch<AutomationSettings>("/api/settings/automation");
}

export function updateAutomationSettings(patch: Partial<AutomationSettings>) {
  return authFetch<AutomationSettings>("/api/settings/automation", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}

// ---- Locker / pickup-point network ----

export interface Locker {
  courier: string;
  id: string;
  name: string;
  address: string;
  city: string;
  county: string;
  zip: string;
  external?: boolean;
}

export function getLockers(params: { courier?: string; q?: string; city?: string; limit?: number } = {}) {
  const qs = new URLSearchParams();
  if (params.courier) qs.set("courier", params.courier);
  if (params.q) qs.set("q", params.q);
  if (params.city) qs.set("city", params.city);
  if (params.limit) qs.set("limit", String(params.limit));
  const query = qs.toString();
  return authFetch<{ lockers: Locker[]; total: number }>(`/api/lockers${query ? `?${query}` : ""}`);
}

export function syncStatuses() {
  return authFetch<{ status: string; processed: number; actions: Record<string, number> }>(
    "/api/awb/sync-statuses",
    { method: "POST" },
  );
}

// ---- Courier actions (create / bulk / void / label) ----

export interface CreatedAwb {
  order_id: number;
  order_name: string | null;
  awb: string;
  courier: string;
}
export interface BulkAwbResponse {
  success: boolean;
  created: CreatedAwb[];
  errors: { order_id: number | string; error: string }[];
  total: number;
  pickup?: { supported?: boolean; requested?: boolean; message?: string } | null;
}
export interface AwbCreateOptions {
  label_size?: string;      // A4 | A6
  address_id?: string;      // pickup point / locker id
  service_id?: number;
  parcels_count?: number;
  total_weight?: number;
  // Per-order overrides (win over the saved profile in the backend resolver):
  width?: number;
  height?: number;
  length?: number;
  content?: string;
  cod_amount?: number;
  declared_value?: number;
}

/** Create AWBs for many orders. Pass `profile_id` to apply a saved profile (courier + parcels +
 * weight + dims + content); `account_key` alone still works for an ad-hoc courier pick. */
export function createBulkAwb(
  order_ids: number[],
  account_key: string,
  options?: AwbCreateOptions,
  profile_id?: number,
) {
  return authFetch<BulkAwbResponse>("/api/awb/bulk", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ order_ids, account_key, options: options ?? {}, profile_id }),
  });
}

export interface SplitAwbResponse {
  success: boolean;
  split: boolean;
  locations: number;
  created: { location: string; awb: string; fulfillment_order_id: string }[];
  errors: { location: string | null; error: string }[];
  pickup?: { supported?: boolean; requested?: boolean; message?: string } | null;
}

/** One AWB per fulfillment location for an order split across 2+ locations. */
export function splitAwb(
  order_id: number,
  account_key: string,
  options?: AwbCreateOptions,
  profile_id?: number,
) {
  return authFetch<SplitAwbResponse>("/api/awb/split", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ order_id, account_key, options: options ?? {}, profile_id }),
  });
}

export function voidAwb(shipment_id: number) {
  return authFetch<{ success: boolean; voided_awb: string }>("/api/awb/void", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ shipment_id }),
  });
}

/** Fetch the label PDF (auth) and open it in a new tab for printing. */
export async function printLabel(shipment_id: number, size = "A6"): Promise<void> {
  const token = await shopify.idToken();
  const res = await fetch(`/api/awb/label?shipment_id=${shipment_id}&size=${encodeURIComponent(size)}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    let msg = `Label request failed (${res.status})`;
    try {
      const b = (await res.json()) as { detail?: string };
      msg = b.detail ?? msg;
    } catch { /* keep */ }
    throw new ApiError(res.status, msg);
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  window.open(url, "_blank");
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

/** `GET /api/overview` — Home dashboard signals. */
export interface OverviewResponse {
  orders_total: number;
  address_issues: number;
  print_queue: number;
  has_courier_account: boolean;
  test_mode: boolean;
  test_mode_used: boolean;
  has_sender: boolean;
  has_packing: boolean;
  has_profile: boolean;
  has_invoicing: boolean;
  has_awb: boolean;
  auto_awb_enabled: boolean;
  plan: string;
  last_sync_at: string | null;
  syncing: boolean;
}

export function getOverview() {
  return authFetch<OverviewResponse>("/api/overview");
}

// ---- Picking / packing list ----
export interface PickAggregate {
  sku: string | null; title: string | null; total_qty: number; order_count: number;
  location: string | null; shelf: string | null; shelf_position: string | null;
}
export interface PackOrder {
  id: number; name: string | null; city: string | null; customer: string | null;
  awb: string | null; courier: string | null; printed: boolean; store_name: string | null;
  items: { sku: string | null; title: string | null; quantity: number | null }[];
}
export interface PickingListResponse {
  aggregate: PickAggregate[]; orders: PackOrder[];
  total_orders: number; total_units: number; total_skus: number;
}
export function getPickingList(
  params: { scope?: string; lens?: string; date_from?: string; date_to?: string } = {},
) {
  const qs = new URLSearchParams();
  if (params.scope) qs.set("scope", params.scope);
  if (params.lens) qs.set("lens", params.lens);
  if (params.date_from) qs.set("date_from", params.date_from);
  if (params.date_to) qs.set("date_to", params.date_to);
  const q = qs.toString();
  return authFetch<PickingListResponse>(`/api/picking-list${q ? `?${q}` : ""}`);
}

/** `GET /api/address-issues` — orders whose shipping address needs attention. */
export interface AddressIssue {
  id: number;
  name: string | null;
  customer: string | null;
  address_status: string | null;
  address_score: number | null;
  shipping: {
    name: string | null;
    address1: string | null;
    address2: string | null;
    city: string | null;
    zip: string | null;
    province: string | null;
    country: string | null;
    phone: string | null;
  };
  errors: unknown;
  suggestions: unknown;
}
export interface AddressIssuesResponse {
  issues: AddressIssue[];
  total: number;
  page: number;
  per_page: number;
}

export function getAddressIssues(page = 1) {
  return authFetch<AddressIssuesResponse>(`/api/address-issues?page=${page}`);
}

/** `GET /api/print-queue` — orders with an AWB ready to print. */
export interface PrintShipment {
  id: number;
  awb: string | null;
  courier: string | null;
  paper_size: string | null;
}
export interface PrintItem {
  id: number;
  name: string | null;
  customer: string | null;
  city: string | null;
  shipments: PrintShipment[];
}
export interface PrintQueueResponse {
  items: PrintItem[];
  total: number;
  page: number;
  per_page: number;
}

export function getPrintQueue(page = 1) {
  return authFetch<PrintQueueResponse>(`/api/print-queue?page=${page}`);
}

// ---- Print ops: filtered queue, prepared batches, rules, logs ----
export interface PrintCriteria {
  sku?: string; qty?: number; tag?: string; date?: string;
  date_from?: string; date_to?: string;
}
export interface PrintQueueItem {
  id: number; name: string | null; customer: string | null; city: string | null;
  store_name: string | null;
  shipments: { id: number; awb: string | null; courier: string | null }[];
  items: { sku: string | null; title: string | null; quantity: number | null }[];
  unprinted_awbs: string[];
}
export interface PrintQueueResult {
  items: PrintQueueItem[]; total: number; shown: number; cap: number; criteria: PrintCriteria;
}
export interface PrintBatch {
  id: number; name: string; criteria: PrintCriteria; order_count: number; awb_count: number;
  status: string; source: string; created_by: string | null;
  created_at: string | null; printed_at: string | null;
}
export interface PrintLogRow {
  id: number; name: string | null; awb_count: number | null; created_at: string | null;
  summary: { sku: string | null; title: string | null; qty: number }[];
}
export interface PrintRule { id: number; name: string; criteria: PrintCriteria; enabled: boolean }

function qstr(params: Record<string, string | number | undefined>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
}

export function getPrintQueueFiltered(c: PrintCriteria & { scope?: string; limit?: number } = {}) {
  return authFetch<PrintQueueResult>(`/api/print/queue${qstr({ ...c })}`);
}
export function preparePrintBatch(body: { name?: string; scope?: string; criteria?: PrintCriteria; command?: string; source?: string }) {
  return authFetch<PrintBatch>(`/api/print/prepare`, { method: "POST", body: JSON.stringify(body) });
}
export function listPrintBatches(status = "pending") {
  return authFetch<{ batches: PrintBatch[] }>(`/api/print/batches?status=${status}`);
}
export function cancelPrintBatch(id: number) {
  return authFetch<{ success: boolean }>(`/api/print/batches/${id}`, { method: "DELETE" });
}
export function getPrintLogs(limit = 30) {
  return authFetch<{ logs: PrintLogRow[] }>(`/api/print/logs?limit=${limit}`);
}
export function listPrintRules() {
  return authFetch<{ rules: PrintRule[] }>(`/api/print/rules`);
}
export function savePrintRule(body: { name: string; criteria?: PrintCriteria; command?: string }) {
  return authFetch<PrintRule>(`/api/print/rules`, { method: "POST", body: JSON.stringify(body) });
}
export function deletePrintRule(id: number) {
  return authFetch<{ success: boolean }>(`/api/print/rules/${id}`, { method: "DELETE" });
}

/** POST that returns a PDF blob → open it in a new tab for printing. Returns false if blocked. */
async function postPdfBlob(url: string, body: unknown): Promise<void> {
  const token = await shopify.idToken();
  const res = await fetch(url, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) {
    let msg = `Print failed (${res.status})`;
    try { const b = (await res.json()) as { detail?: string }; msg = b.detail ?? msg; } catch { /* keep */ }
    throw new ApiError(res.status, msg);
  }
  const blob = await res.blob();
  const u = URL.createObjectURL(blob);
  window.open(u, "_blank");
  setTimeout(() => URL.revokeObjectURL(u), 60_000);
}
export function printPreparedBatch(id: number) {
  return postPdfBlob(`/api/print/batches/${id}/print`, {});
}
export function printSelection(order_ids: number[]) {
  return postPdfBlob(`/api/print/run`, { order_ids });
}

/** `GET /api/billing` — plans + the shop's current plan. */
export interface BillingPlan {
  key: string;
  name: string;
  price: number;
  trial_days: number;
  order_limit: number | null;
  features: string[];
}
export interface BillingStatus {
  current_plan: string;
  subscription_status: string | null;
  test: boolean;
  plans: BillingPlan[];
}

export function getBilling() {
  return authFetch<BillingStatus>("/api/billing");
}

export function subscribe(plan: string) {
  return authFetch<{ confirmationUrl: string }>(`/api/billing/subscribe?plan=${encodeURIComponent(plan)}`, {
    method: "POST",
  });
}

export function cancelBilling() {
  return authFetch<{ current_plan: string }>("/api/billing/cancel", { method: "POST" });
}

// ---- Per-order actions (Orders page + CS backlog) ----

function post<T>(path: string, body?: unknown) {
  return authFetch<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
}

export function holdOrder(orderId: number, reason?: string, notes?: string) {
  return post<{ success: boolean; held: number; on_hold: boolean }>(
    `/api/orders/${orderId}/hold`, { reason, notes });
}
export function releaseOrder(orderId: number) {
  return post<{ success: boolean; released: number; on_hold: boolean }>(`/api/orders/${orderId}/release`);
}
export function cancelOrderAction(
  orderId: number,
  opts: { reason?: string; refund?: boolean; restock?: boolean; notify_customer?: boolean; staff_note?: string } = {},
) {
  return post<{ success: boolean; cancelled: boolean }>(`/api/orders/${orderId}/cancel`, opts);
}
export function setOrderNote(orderId: number, note: string) {
  return post<{ success: boolean; note: string }>(`/api/orders/${orderId}/note`, { note });
}
export function editOrderTags(orderId: number, add: string[], remove: string[]) {
  return post<{ success: boolean; tags: string }>(`/api/orders/${orderId}/tags`, { add, remove });
}
export function emailCustomer(
  orderId: number,
  payload: { template_id?: number; subject?: string; body?: string; to?: string },
) {
  return post<{ success: boolean; sent: boolean; subject: string }>(`/api/orders/${orderId}/email`, payload);
}

// ---- Order editing (Order Editing API) + swap ----

export interface EditabilityLine {
  line_item_id: string; title: string | null; sku: string | null;
  quantity: number | null; variant_id: string | null;
}
export interface Editability {
  editable: boolean; reason: string | null; gateways: string[]; cancelled: boolean;
  line_items: EditabilityLine[];
}
export function getEditability(orderId: number) {
  return authFetch<Editability>(`/api/orders/${orderId}/editability`);
}
export interface EditBeginResult {
  success: boolean; calc_order_id: string;
  line_items: { id: string; title: string | null; sku: string | null; quantity: number | null; editable_quantity: number | null }[];
  shipping_lines: { id: string; title: string | null }[];
}
export function orderEditBegin(orderId: number) {
  return post<EditBeginResult>(`/api/orders/${orderId}/edit/begin`);
}
export interface OrderEditChanges {
  calc_order_id: string;
  set_qty?: { line_item_id: string; quantity: number }[];
  add_variants?: { variant_id: string; quantity: number }[];
  line_discounts?: { line_item_id: string; percent?: number; amount?: string; description?: string }[];
  remove_shipping_ids?: string[];
  notify?: boolean;
  staff_note?: string;
}
export function orderEditCommit(orderId: number, changes: OrderEditChanges) {
  return post<{ success: boolean; order_id: string }>(`/api/orders/${orderId}/edit/commit`, changes);
}
export function orderSwap(
  orderId: number,
  payload: { line_items: { variant_id: string; quantity: number }[]; note?: string; cancel_original?: boolean; notify?: boolean; tags?: string[] },
) {
  return post<{ success: boolean; order_id: string; name: string; cancelled_original: boolean }>(
    `/api/orders/${orderId}/swap`, payload);
}

export interface AddressFields {
  name?: string;
  address1?: string;
  address2?: string;
  city?: string;
  zip?: string;
  province?: string;
  country?: string;
  phone?: string;
}
/** Save the shipping address to Shopify + re-validate. Works for every order (incl. Releaseit). */
export function editOrderAddress(orderId: number, fields: AddressFields) {
  return post<{ success: boolean; revalidated: boolean; is_valid?: boolean;
    address_status: string | null; address_score?: number | null; errors?: unknown }>(
    `/api/orders/${orderId}/address`, fields);
}

// ---- Address intelligence: live anomalies + as-you-type suggestions (RO nomenclator) ----
export interface AddressAnomaly {
  field: string; issue: string; recommendation: string; blocking?: boolean;
  fix?: Partial<AddressFields>;
}
export interface AddressAnalysis {
  status: string; valid: boolean | null; score: number | null;
  anomalies: AddressAnomaly[]; suggestions: string[]; recommended_zip: string | null;
}
export interface AddressSuggestion {
  label: string; street?: string; city?: string; province?: string; zip?: string | null;
}
/** Live, non-persisting analysis of an ad-hoc address → structured anomalies + recommended fixes. */
export function analyzeAddress(fields: AddressFields) {
  return post<AddressAnalysis>(`/api/address/analyze`, fields);
}
/** As-you-type completion from the RO nomenclator. field = street | city | zip. */
export function suggestAddress(
  q: string, field: "street" | "city" | "zip",
  ctx: { city?: string; province?: string; zip?: string } = {},
) {
  const p = new URLSearchParams({ q, field });
  if (ctx.city) p.set("city", ctx.city);
  if (ctx.province) p.set("province", ctx.province);
  if (ctx.zip) p.set("zip", ctx.zip);
  return authFetch<{ suggestions: AddressSuggestion[] }>(`/api/address/suggest?${p.toString()}`);
}
/** Mark the address correct (override validator) + create the AWB now; falls to CS on failure. */
export function shipNow(orderId: number, opts: { profile_id?: number; account_key?: string; mark_correct?: boolean } = {}) {
  return post<{ success: boolean; awb?: string; courier?: string; sent_to_cs?: boolean; error?: string }>(
    `/api/orders/${orderId}/ship-now`, opts);
}
/** Attach an externally-created AWB (made on the courier site) — tracking + label work via the
 * chosen courier account, same as our own AWBs. */
export function attachManualAwb(
  orderId: number,
  payload: { awb: string; account_key: string; tracking_url?: string; fulfill?: boolean; notify?: boolean },
) {
  return post<{ success: boolean; shipment_id: number; awb: string; courier: string;
    fulfilled: boolean; tracking_url: string; note: string }>(`/api/orders/${orderId}/manual-awb`, payload);
}
/** Mark the order PAID in Shopify (e.g. COD collected). */
export function markPaid(orderId: number) {
  return post<{ success: boolean; financial_status: string }>(`/api/orders/${orderId}/mark-paid`);
}
/** Is SmartBill invoicing configured on the server? */
export function smartbillStatus() {
  return authFetch<{ configured: boolean }>(`/api/smartbill/status`);
}
/** Cancel / storno / delete the order's fiscal invoice. */
export function cancelInvoice(orderId: number, mode: "cancel" | "storno" | "delete" = "cancel") {
  return authFetch<{ success: boolean; mode: string; series: string; number: string }>(
    `/api/orders/${orderId}/invoice/cancel`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode }) });
}

/** Create a SmartBill fiscal invoice for the order (itemized from Shopify line prices). */
export function createInvoice(orderId: number, opts: { draft?: boolean; send_email?: boolean; series?: string } = {}) {
  return post<{ success: boolean; series: string | null; number: string | null; url: string | null; is_draft: boolean; emailed: boolean }>(
    `/api/orders/${orderId}/invoice`, opts);
}
export interface InvoiceSettings {
  series: string; vat_rate: number; vat_name: string; currency: string; due_days: number;
  measure_unit: string; save_product: boolean; save_customer: boolean; product_code_type: string;
  extra_cif: string; use_estimate: boolean; auto_send_email: boolean; mentions: string;
  // advanced VAT / invoice lines
  vat_category: string; prices_include_vat: boolean; vat_on_payment: boolean;
  client_tax_payer: boolean; default_product_code: string;
  invoice_shipping: boolean; shipping_name: string; shipping_code: string;
  shipping_vat_rate: number | null;
  // which connector issues invoices: "xconnector" (store's SmartBill via xConnector) | "smartbill" (server)
  invoice_via?: string;
}
export function getInvoiceSettings() {
  return authFetch<InvoiceSettings>(`/api/invoice-settings`);
}
export function saveInvoiceSettings(s: Partial<InvoiceSettings>) {
  return authFetch<{ success: boolean; invoice_settings: InvoiceSettings }>(`/api/invoice-settings`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(s),
  });
}

// --- Automation schedule (per store: mode on_order|cron|on_delivered|off + minutes; risk actions) ---
export interface ScheduleEntry { mode: string; minutes: number; }
export interface SpecialRule { contains: string; action: string; }
export interface AutomationSchedule {
  duplicates: ScheduleEntry; parcels: ScheduleEntry; surprise: ScheduleEntry;
  blocklist: ScheduleEntry; special: ScheduleEntry; cod_capture: ScheduleEntry; awb: ScheduleEntry;
  no_hold?: boolean;
  fulfill_when?: string;
  special_rules?: SpecialRule[];
}
export function getAutomationSchedule() {
  return authFetch<AutomationSchedule>(`/api/automation-schedule`);
}
export function saveAutomationSchedule(s: Partial<AutomationSchedule>) {
  return authFetch<{ success: boolean; automation_schedule: AutomationSchedule }>(`/api/automation-schedule`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(s),
  });
}
export function getSmartbillSeries() {
  return authFetch<{ configured: boolean; series: { name: string; next: string | number }[]; error?: string }>(
    `/api/smartbill/series`);
}
/** Mark the order DELIVERED in Shopify (pushes a DELIVERED delivery event). */
export function markDelivered(orderId: number, notify = false) {
  return post<{ success: boolean; delivered: boolean; event: string | null }>(
    `/api/orders/${orderId}/mark-delivered`, { notify });
}

/** Bulk "these look fine — ship them all" (override validator); anything that can't ship → CS. */
export function shipNowBulk(order_ids: number[], opts: { profile_id?: number; account_key?: string } = {}) {
  return post<{ success: boolean;
    shipped: { order_id?: number; awb?: string; courier?: string }[];
    sent_to_cs: { order_id: number; error?: string }[];
    skipped: { order_id: number | string; error: string }[];
    counts: { shipped: number; sent_to_cs: number; skipped: number };
  }>("/api/orders/ship-now-bulk", { order_ids, ...opts });
}

// ---- CS backlog / call-queue ----

export type CSReason = "wrong_address" | "rule" | "duplicate" | "out_of_stock" | "manual";
export type CSStatus = "open" | "in_progress" | "solved";

export interface CSOrderBrief {
  id: number;
  name: string | null;
  customer: string | null;
  shipping_name: string | null;
  total_price: number | null;
  financial_status: string | null;
  financial_paid: boolean;
  created_at: string | null;
  address_status: string | null;
  address_score: number | null;
  city: string | null;
  zip: string | null;
  phone: string | null;
  email: string | null;
  province: string | null;
  country: string | null;
  address1: string | null;
  address2: string | null;
  address_full: string;
  units: number;
  line_count: number;
  fulfilled: boolean;
  awb: string | null;
  courier: string | null;
  shipment_id: number | null;
  on_hold: boolean;
  cancelled: boolean;
  note: string | null;
  tags: string[];
  store_domain: string | null;
  store_name: string | null;
  admin_url: string | null;
}
export interface CSNote { at: string; text: string; by: string }
export interface CSQueueItem {
  id: number;
  order_id: number;
  reason: CSReason;
  reason_detail: string | null;
  status: CSStatus;
  priority: number | null;
  was_held: boolean;
  created_by: string;
  created_at: string | null;
  solved_at: string | null;
  notes: CSNote[];
  order: CSOrderBrief;
}
export interface CSQueueResponse {
  items: CSQueueItem[];
  total: number;
  counts: Record<string, number>;
}
export interface CSPanel extends CSQueueItem {
  line_items: { sku: string | null; title: string | null; quantity: number | null }[];
  shipments: { id: number; awb: string | null; courier: string | null; last_status: string | null; paper_size: string | null }[];
  other_orders: {
    id: number; name: string | null; total_price: number | null; financial_status: string | null;
    created_at: string | null; city: string | null; awb: string | null; cancelled: boolean;
  }[];
  other_orders_count: number;
  email_templates: CSEmailTemplate[];
}

export function listCSQueue(
  params: { status?: CSStatus; reason?: CSReason; scope?: string; sort?: string; include_solved?: boolean } = {},
) {
  const qs = new URLSearchParams();
  if (params.status) qs.set("status", params.status);
  if (params.reason) qs.set("reason", params.reason);
  if (params.scope) qs.set("scope", params.scope);
  if (params.sort) qs.set("sort", params.sort);
  if (params.include_solved) qs.set("include_solved", "true");
  const q = qs.toString();
  return authFetch<CSQueueResponse>(`/api/cs-queue${q ? `?${q}` : ""}`);
}
export function getCSPanel(itemId: number) {
  return authFetch<CSPanel>(`/api/cs-queue/${itemId}`);
}
export function addToCSQueue(order_id: number, reason?: CSReason, detail?: string, hold?: boolean) {
  return post<CSQueueItem & { success: boolean }>("/api/cs-queue/add", { order_id, reason, detail, hold });
}
export function bulkAddToCSQueue(order_ids: number[], reason?: CSReason, detail?: string, hold?: boolean) {
  return post<{ success: boolean; added: number }>("/api/cs-queue/bulk-add", { order_ids, reason, detail, hold });
}
export function setCSStatus(itemId: number, status: CSStatus) {
  return post<{ success: boolean; status: CSStatus }>(`/api/cs-queue/${itemId}/status`, { status });
}
export function addCSNote(itemId: number, text: string) {
  return post<{ success: boolean; notes: CSNote[] }>(`/api/cs-queue/${itemId}/note`, { text });
}
export function setCSPriority(itemId: number, priority: number) {
  return post<{ success: boolean; priority: number }>(`/api/cs-queue/${itemId}/priority`, { priority });
}
export function resolveCSAwb(itemId: number, opts: { profile_id?: number; account_key?: string } = {}) {
  return post<{ success: boolean; awb: string; courier: string; status: string }>(
    `/api/cs-queue/${itemId}/resolve-awb`, opts);
}
export function removeCSItem(itemId: number, keep_hold?: boolean) {
  return post<{ success: boolean; removed: boolean }>(`/api/cs-queue/${itemId}/remove`, { keep_hold });
}
export function scanCSQueue(scope?: string) {
  const q = scope ? `?scope=${encodeURIComponent(scope)}` : "";
  return post<{ success: boolean; scanned: number; enqueued: Record<string, number> }>(`/api/cs-queue/scan${q}`);
}
export function scanCSQueueOOS(scope?: string) {
  const q = scope ? `?scope=${encodeURIComponent(scope)}` : "";
  return post<{ success: boolean; enqueued: number; short_skus: number; checked: number; message?: string }>(
    `/api/cs-queue/scan-oos${q}`);
}
export interface CSProductCandidate {
  id: number; name: string | null; customer: string | null; city: string | null;
  total_price: number | null; created_at: string | null; address_status: string | null;
  units: number; matched: { sku: string | null; title: string | null; quantity: number | null }[];
  queued: boolean; store_name: string | null;
}
export function csProductSearch(q: string, scope?: string) {
  return post<{ candidates: CSProductCandidate[]; total: number }>("/api/cs-queue/product-search", { q, scope });
}

// ---- CS settings + email templates ----

export interface CSSettings {
  auto_hold?: boolean;
  auto_enqueue_wrong_address?: boolean;
  value_min?: number | null;
  products_any?: string[];
  tags_any?: string[];
  categories_any?: string[];
  duplicate_enabled?: boolean;
  duplicate_window_hours?: number;
  duplicate_match?: string;
  flag_tag?: string | null;
  hold_reason?: string | null;
}
export function getCSSettings() {
  return authFetch<{ settings: CSSettings }>("/api/cs-settings");
}
export function putCSSettings(patch: CSSettings) {
  return authFetch<{ success: boolean; settings: CSSettings }>("/api/cs-settings", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch),
  });
}

export interface CSEmailTemplate {
  description?: string | null;
  id: number;
  name: string;
  subject: string;
  body: string;
  auto_on: string | null;
  is_active?: boolean;
}
export type CSEmailTemplateInput = Omit<CSEmailTemplate, "id">;
export function listCSTemplates() {
  return authFetch<{ templates: CSEmailTemplate[] }>("/api/cs-email-templates");
}
/** Create the starter CS template set (skips names the store already has). */
export function seedCSTemplates() {
  return post<{ success: boolean; created: number; skipped: number }>("/api/cs-email-templates/seed-defaults");
}
export function createCSTemplate(input: CSEmailTemplateInput) {
  return post<CSEmailTemplate & { success: boolean }>("/api/cs-email-templates", input);
}
export function updateCSTemplate(id: number, input: Partial<CSEmailTemplateInput>) {
  return authFetch<CSEmailTemplate & { success: boolean }>(`/api/cs-email-templates/${id}`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input),
  });
}
export function deleteCSTemplate(id: number) {
  return authFetch<{ success: boolean; deleted: boolean }>(`/api/cs-email-templates/${id}`, { method: "DELETE" });
}

// ---- Saved picking lists ----
export interface PickingListRow {
  id: number; name: string; lens: string | null; scope: string | null;
  order_ids: number[]; total_orders: number; total_units: number; total_skus: number;
  status: "open" | "picked" | "cancelled"; created_at: string | null; completed_at: string | null;
}
export function listPickingLists(status?: string) {
  const q = status ? `?status=${encodeURIComponent(status)}` : "";
  return authFetch<{ lists: PickingListRow[] }>(`/api/picking/lists${q}`);
}
export function createPickingList(body: { name?: string; lens?: string; scope?: string }) {
  return authFetch<{ success: boolean; list: PickingListRow }>("/api/picking/lists", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}
export function getPickingListDetail(id: number) {
  return authFetch<PickingListResponse & { list: PickingListRow }>(`/api/picking/lists/${id}`);
}
export function setPickingListStatus(id: number, status: "open" | "picked" | "cancelled") {
  return authFetch<{ success: boolean; list: PickingListRow }>(`/api/picking/lists/${id}/status`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status }) });
}

/** A printable A6 barcode label for rehearsing the warehouse scan flow. Returns a PDF blob. */
export function scanTestLabel(body: { order_id?: number; source?: "order" | "awb"; attach_fake_awb?: boolean } = {}) {
  return authFetchBlob("/api/scan/test-label", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

// ---- Test mode (sandbox) ----
export interface TestModeState {
  test_mode: boolean;
  test_mode_used?: boolean;
  created?: Record<string, number>;
  removed?: Record<string, number>;
}
export function getTestMode() {
  return authFetch<TestModeState>("/api/test-mode");
}
export function setTestMode(enabled: boolean) {
  return authFetch<TestModeState>("/api/test-mode", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }) });
}
export function reseedTestMode() {
  return post<TestModeState>("/api/test-mode/reseed");
}

/* ================== Address Lab (consolidated validator + rules + policies) ================== */

export type AddressCheckInput = {
  country?: string; province?: string; city?: string; zip?: string;
  address1?: string; address2?: string;
};
export type AddressCheckResult = {
  input: Required<AddressCheckInput>;
  status: "valid" | "corrected" | "needs_geocoder" | "cs";
  address: { province?: string; city?: string; zip?: string; address1?: string } | null;
  source: string;
  note: string;
};
export async function checkAddress(fields: AddressCheckInput): Promise<AddressCheckResult> {
  return authFetch<AddressCheckResult>("/api/address/check", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
}

export type ValidationRule = { id: string; scope: string; name: string; desc: string };
export type PolicyMeta = {
  key: string; type: "bool" | "int" | "float" | "enum";
  label: string; help: string; options?: string[];
};
export type ValidationRulesResponse = {
  rules: ValidationRule[];
  policy_meta: PolicyMeta[];
  policy_defaults: Record<string, unknown>;
};
export async function getValidationRules(): Promise<ValidationRulesResponse> {
  return authFetch<ValidationRulesResponse>("/api/validation/rules");
}

export type ValidationPoliciesResponse = {
  defaults: Record<string, unknown>;
  overrides: Record<string, unknown>;
  effective: Record<string, unknown>;
};
export async function getValidationPolicies(): Promise<ValidationPoliciesResponse> {
  return authFetch<ValidationPoliciesResponse>("/api/validation/policies");
}
export async function putValidationPolicies(
  overrides: Record<string, unknown>,
): Promise<{ overrides: Record<string, unknown>; effective: Record<string, unknown> }> {
  return authFetch("/api/validation/policies", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ overrides }),
  });
}

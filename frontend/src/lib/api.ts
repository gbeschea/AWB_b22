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

/** Shape of `GET /api/me`. */
export interface MeResponse {
  shop: string;
  name: string;
  is_active: boolean;
  api_version: string;
  plan: string;
}

/** One row of `GET /api/orders`. */
export interface OrderRow {
  id: number;
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
  awb: string | null;
  courier: string | null;
  last_status: string | null;
  printed: boolean;
}

export interface OrdersResponse {
  orders: OrderRow[];
  total: number;
  page: number;
  per_page: number;
}

export function listOrders(params: { page?: number; q?: string; status?: string } = {}) {
  const qs = new URLSearchParams();
  if (params.page) qs.set("page", String(params.page));
  if (params.q) qs.set("q", params.q);
  if (params.status) qs.set("status", params.status);
  const query = qs.toString();
  return authFetch<OrdersResponse>(`/api/orders${query ? `?${query}` : ""}`);
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
  default_service_id: number | null;
}
export interface CouriersResponse {
  accounts: CourierAccount[];
  mappings: CourierMapping[];
  profiles: ShipmentProfile[];
}

export function getCouriers() {
  return authFetch<CouriersResponse>("/api/couriers");
}

/** `GET /api/overview` — Home dashboard signals. */
export interface OverviewResponse {
  orders_total: number;
  address_issues: number;
  print_queue: number;
  has_courier_account: boolean;
  plan: string;
  last_sync_at: string | null;
  syncing: boolean;
}

export function getOverview() {
  return authFetch<OverviewResponse>("/api/overview");
}

/** `POST /api/sync` — trigger a background backfill of recent orders for this shop. */
export interface SyncResponse {
  status: "started" | "in_progress";
  since_days?: number;
}

export function syncNow() {
  return authFetch<SyncResponse>("/api/sync", { method: "POST" });
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

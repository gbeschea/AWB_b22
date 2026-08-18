import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  ActionList,
  Badge,
  Banner,
  Box,
  Divider,
  BlockStack,
  Button,
  ButtonGroup,
  Card,
  Checkbox,
  ChoiceList,
  EmptyState,
  Filters,
  IndexTable,
  InlineStack,
  Modal,
  Page,
  Pagination,
  Popover,
  Select,
  SkeletonBodyText,
  Tabs,
  Text,
  TextField,
  useIndexResourceState,
} from "@shopify/polaris";
import {
  ApiError,
  addToCSQueue,
  cancelOrderAction,
  createBulkAwb,
  createProfile,
  getCouriers,
  getLockers,
  createInvoice,
  cancelInvoice,
  getOrderDetail,
  getOrderTimeline,
  getOrg,
  getProductTags,
  backfillProductTags,
  holdOrder,
  smartbillStatus,
  pickSheet,
  listOrders,
  markDelivered,
  markPaid,
  printLabel,
  releaseOrder,
  splitAwb,
  syncStatuses,
  voidAwb,
  type BulkAwbResponse,
  type CourierAccount,
  type CreatedAwb,
  type Locker,
  type OrderDetail,
  type OrderFilters,
  type OrderRow,
  type OrdersResponse,
  type OrgResponse,
  type ShipmentProfile,
  type TimelineEvent,
} from "../lib/api";
import { t } from "../lib/i18n";
import { setOrderNote } from "../lib/api";
import { OrderEditModal } from "../components/OrderEditModal";
import { ManualAwbModal } from "../components/ManualAwbModal";
import { AddressEditor } from "../components/AddressEditor";
import { useCommandBus } from "../lib/commandBus";

const LAST_PROFILE_KEY = "orderhub.lastProfileId"; // sticky: remembers the operator's last profile

const PER_PAGE_OPTIONS = [
  { label: "25 / page", value: "25" },
  { label: "50 / page", value: "50" },
  { label: "100 / page", value: "100" },
  { label: "200 / page", value: "200" },
];
const SIZE_OPTIONS = [
  { label: "A6 (thermal)", value: "A6" },
  { label: "A4", value: "A4" },
];

type AddressTone = "success" | "warning" | "critical" | "attention" | undefined;

function addressTone(status: string | null): AddressTone {
  switch (status) {
    case "valid":
      return "success";
    case "partial_match":
      return "attention";
    case "invalid":
    case "not_found":
      return "critical";
    default:
      return "warning";
  }
}

type StatusTone = "info" | "success" | "warning" | "critical" | "attention" | "new" | undefined;

// One clean, emoji-free lifecycle status per order, each with its own colour. The Status column
// shows where the PARCEL is — address validation lives in its own column, so we never surface
// raw "pending_validation" here (an unshipped order is simply "New").
function orderStatus(o: OrderRow): { label: string; tone: StatusTone } {
  const s = `${o.derived_status ?? ""} ${o.processing_status ?? ""} ${o.last_status ?? ""}`.toLowerCase();
  const h = (...xs: string[]) => xs.some((x) => s.includes(x));
  if (h("anul", "cancel")) return { label: "Cancelled", tone: undefined };
  if (h("refuz", "return", "retur")) return { label: "Refused", tone: "critical" };
  if (h("livrat", "delivered")) return { label: "Delivered", tone: "success" };
  if (h("curs de livr", "in curs", "tranzit", "transit", "out for delivery", "livrare")) return { label: "In transit", tone: "attention" };
  if (h("expedi", "shipped", "warehouse", "pickup")) return { label: "Shipped", tone: "attention" };
  if (o.in_cs) return { label: "Sent to CS", tone: "attention" };
  if (h("netrimis", "alert", "ⁿ")) return { label: "Not shipped", tone: "warning" };
  if (h("hold") || o.on_hold) return { label: "On hold", tone: "warning" };
  // An order that has an AWB is processed — regardless of a stale processing_status. This must
  // win over the "neproces"/default "New" fallbacks (test/webhook shipments leave the order at
  // pending_validation, which otherwise reads as "New" despite the AWB).
  if (o.awb) return { label: "Processed", tone: "info" };
  return { label: "New", tone: "new" };
}

function money(v: number | null): string {
  return v == null ? "—" : v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// Shopify timeline messages come back as HTML — render them as plain text (safe, no XSS).
function stripHtml(s: string | null): string {
  if (!s) return "";
  return s.replace(/<[^>]*>/g, "").replace(/&amp;/g, "&").replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/\s+/g, " ").trim();
}
function whenShort(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? "" : d.toLocaleString(undefined, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}

function toast(msg: string, isError = false) {
  (window as any).shopify?.toast?.show(msg, isError ? { isError: true } : undefined);
}

// --- Command box (in-app command palette) — deterministic parser, no LLM. ---
// `key:value` tokens set faceted filters; whole-phrase words switch lens or navigate; "awb <order>
// [courier]" opens the (confirm-gated) create dialog. NO auto-execution of destructive actions.
const CMD_FILTER_KEYS: Record<string, keyof OrderFilters> = {
  tag: "tag", product: "product", sku: "product",
  producttag: "product_tag", ptag: "product_tag", "product-tag": "product_tag",
  city: "city", county: "province", judet: "province", province: "province", courier: "courier",
};
const CMD_LENS: Record<string, string> = {
  unfulfilled: "unfulfilled", "to ship": "unfulfilled", toship: "unfulfilled", "de expediat": "unfulfilled",
  fulfilled: "fulfilled", awb: "fulfilled", "in transit": "in_transit", transit: "in_transit",
  delivered: "delivered", livrate: "delivered", refused: "refused", refuzate: "refused", all: "all",
};
const CMD_NAV: Record<string, string> = {
  "print queue": "/app/printing", printing: "/app/printing", queue: "/app/printing",
  "address issues": "/app/validation", addresses: "/app/validation", validation: "/app/validation",
  adrese: "/app/validation", overview: "/app", home: "/app", picking: "/app/picking", scan: "/app/scan",
};
// Command-box value normalizers — map free text to the canonical facet values.
const _isoDay = (offset = 0): string => {
  const d = new Date(); d.setDate(d.getDate() + offset); return d.toISOString().slice(0, 10);
};
const _cmdDate = (v: string): string | undefined => {
  const s = v.trim().toLowerCase();
  if (/^\d{4}-\d{2}-\d{2}/.test(s)) return s.slice(0, 10);
  if (s === "today" || s === "azi") return _isoDay(0);
  if (s === "yesterday" || s === "ieri") return _isoDay(-1);
  return undefined;
};
const _cmdDelivery = (v: string): string => /lock|easy|pichet|pachet|packeta|pudo|box/.test(v) ? "locker" : "home";
const _cmdDstatus = (v: string): string =>
  /liv|deliver/.test(v) ? "delivered" : /refuz|retur|refus|return/.test(v) ? "refused"
    : /transit|tranzit|curs/.test(v) ? "in_transit" : /none|fara|no-?awb/.test(v) ? "none" : v;
const _cmdAddr = (v: string): string =>
  /inval|gres/.test(v) ? "invalid" : /valid|ok|bun/.test(v) ? "valid"
    : /part/.test(v) ? "partial_match" : /neval|not/.test(v) ? "nevalidat" : v;

// Parse a command's `key:value` tokens + bare date words into an OrderFilters object (pure — no side
// effects). Returns the filters, a human summary, and the leftover phrase (verbs/lens/search).
function parseCmdFilters(text: string): { filters: OrderFilters; applied: string[]; rest: string } {
  const filters: OrderFilters = {};
  const applied: string[] = [];
  const set = (fk: keyof OrderFilters, fv: string | number): string => {
    (filters as Record<string, unknown>)[fk] = fv; applied.push(`${fk}=${fv}`); return "";
  };
  let rest = text.replace(/(\S+?):("[^"]*"|\S+)/g, (m, key: string, val: string) => {
    const k = key.toLowerCase();
    const v = val.replace(/^"|"$/g, "").toLowerCase();
    if (CMD_FILTER_KEYS[k]) return set(CMD_FILTER_KEYS[k], v);
    if (k === "payment") return set("payment", /card|cart/.test(v) ? "card" : "cod");
    if (k === "printed") return set("printed", /^(y|da|1|true|print)/.test(v) ? "yes" : "no");
    if (["invoiced", "invoice", "factura", "facturat"].includes(k))
      return set("invoiced", /^(y|da|1|true)/.test(v) ? "yes" : "no");
    if (k === "status") return set("order_status", v);
    if (k === "delivery" || k === "livrare") return set("delivery", _cmdDelivery(v));
    if (["delivery-status", "dstatus", "tracking"].includes(k)) return set("delivery_status", _cmdDstatus(v));
    if (["address", "addr", "adresa"].includes(k)) return set("address_status", _cmdAddr(v));
    if (["qty-min", "qtymin", "qmin"].includes(k)) return set("qty_min", Number(v) || 0);
    if (["qty-max", "qtymax", "qmax"].includes(k)) return set("qty_max", Number(v) || 0);
    if (k === "qty") { const [a, b] = v.split(/[-–]/); if (a) set("qty_min", Number(a) || 0); if (b) set("qty_max", Number(b) || 0); return ""; }
    if (["total-min", "value-min", "vmin", "min"].includes(k)) return set("total_min", Number(v) || 0);
    if (["total-max", "value-max", "vmax", "max"].includes(k)) return set("total_max", Number(v) || 0);
    if (["date-from", "from", "since"].includes(k)) { const d = _cmdDate(v); return d ? set("date_from", d) : ""; }
    if (["date-to", "to", "until"].includes(k)) { const d = _cmdDate(v); return d ? set("date_to", d) : ""; }
    return m; // unknown key → leave in the phrase
  }).trim();
  if (/\b(today|azi)\b/.test(rest.toLowerCase())) { set("date_from", _isoDay(0)); rest = rest.replace(/\b(today|azi)\b/i, "").trim(); }
  else if (/\b(yesterday|ieri)\b/.test(rest.toLowerCase())) { const d = _isoDay(-1); set("date_from", d); set("date_to", d); rest = rest.replace(/\b(yesterday|ieri)\b/i, "").trim(); }
  else if (/\b(last7|7d|week|saptamana)\b/.test(rest.toLowerCase())) { set("date_from", _isoDay(-7)); rest = rest.replace(/\b(last7|7d|week|saptamana)\b/i, "").trim(); }
  return { filters, applied, rest };
}

// Faceted-filter option sets (xConnector-style).
const CH_PAYMENT = [{ label: "Card (paid)", value: "card" }, { label: "Ramburs / COD", value: "cod" }];
const CH_DELIVERY = [{ label: "Home delivery", value: "home" }, { label: "Locker / pickup", value: "locker" }];
const CH_PRINTED = [{ label: "Printed", value: "yes" }, { label: "Not printed", value: "no" }];
const CH_INVOICED = [{ label: "Invoiced", value: "yes" }, { label: "Not invoiced", value: "no" }];
const CH_ORDER_STATUS = [
  { label: "Unfulfilled", value: "unfulfilled" }, { label: "AWB created", value: "awb" },
  { label: "On hold", value: "on_hold" }, { label: "Cancelled", value: "cancelled" },
  { label: "Sent to CS", value: "cs" },
];
const CH_DELIVERY_STATUS = [
  { label: "In transit", value: "in_transit" }, { label: "Delivered", value: "delivered" },
  { label: "Refused / return", value: "refused" }, { label: "No AWB", value: "none" },
];
const CH_ADDR_STATUS = [
  { label: "Valid", value: "valid" }, { label: "Invalid", value: "invalid" },
  { label: "Not validated", value: "nevalidat" }, { label: "Partial", value: "partial_match" },
];
const SORT_OPTIONS = [
  { label: "Newest first", value: "date_desc" }, { label: "Oldest first", value: "date_asc" },
  { label: "Total: high → low", value: "total_desc" }, { label: "Total: low → high", value: "total_asc" },
  { label: "Qty: high → low", value: "qty_desc" }, { label: "Qty: low → high", value: "qty_asc" },
  { label: "Order # A→Z", value: "name_asc" }, { label: "Order # Z→A", value: "name_desc" },
];
const _labelOf = (opts: { label: string; value: string }[], v?: string) =>
  opts.find((o) => o.value === v)?.label ?? v ?? "";

type RowMenuItem = { content: string; destructive?: boolean; disabled?: boolean; onAction: () => void };

/** One order row, memoized so toggling selection re-renders ONLY the toggled row (not all 50).
 * Every prop is a primitive or a stable callback; the menu's items are built lazily (only when open). */
/** Every column the Orders table can show. `always` ones can't be switched off. */
type ColId = "order" | "status" | "payment" | "fulfilment" | "store" | "customer" | "date"
  | "total" | "products" | "address" | "awb" | "invoice";
const ALL_COLUMNS: { id: ColId; label: string; always?: boolean; end?: boolean; multiStoreOnly?: boolean }[] = [
  { id: "order", label: t("col.order"), always: true },
  { id: "status", label: t("col.status") },
  { id: "payment", label: t("col.payment") },
  { id: "fulfilment", label: t("col.fulfilment") },
  { id: "store", label: t("col.store"), multiStoreOnly: true },
  { id: "customer", label: t("col.customer") },
  { id: "date", label: t("col.date") },
  { id: "total", label: t("col.total"), end: true },
  { id: "products", label: t("col.products") },
  { id: "address", label: t("col.address") },
  { id: "awb", label: t("col.awb") },
  { id: "invoice", label: t("col.invoice") },
];
const DEFAULT_COLS: ColId[] = ["order", "payment", "fulfilment", "store", "customer",
  "total", "awb", "invoice"];
/** The old Compact / Detailed toggle, kept as one-click presets on top of the column chooser —
 *  the two view modes people already knew, without losing per-column control. */
const COL_PRESETS: { id: "compact" | "default" | "detailed"; label: string; cols: ColId[] }[] = [
  { id: "compact", label: t("cols.compact"), cols: ["order", "payment", "fulfilment", "total", "awb"] },
  { id: "default", label: t("cols.default"), cols: DEFAULT_COLS },
  { id: "detailed", label: t("cols.detailed"),
    cols: ["order", "status", "payment", "fulfilment", "store", "customer", "date", "total",
      "products", "address", "awb", "invoice"] },
];
const COLS_KEY = "orderhub.orders.columns";
const VIEWS_KEY = "orderhub.orders.views";

/** Is the money in? COD that hasn't been collected reads as "COD due", not a scary "Unpaid". */
function paymentState(o: OrderRow): { label: string; tone: StatusTone } {
  const fin = (o.financial_status || "").toLowerCase();
  if (o.financial_paid || fin === "paid") return { label: "Paid", tone: "success" };
  if (fin.includes("refund")) return { label: fin.includes("partial") ? "Part. refunded" : "Refunded", tone: "critical" };
  if (fin === "pending" || !fin) return { label: t("pay.pending"), tone: "attention" };
  if (fin === "authorized") return { label: "Authorized", tone: "info" };
  return { label: fin.replace(/_/g, " "), tone: "warning" };
}

/** Where the parcel is in OUR pipeline — deliberately separate from payment. */
function fulfilmentState(o: OrderRow): { label: string; tone: StatusTone } {
  const st = orderStatus(o);
  if (["Delivered", "Refused", "In transit", "Cancelled"].includes(st.label)) return st;
  if (o.awb) return { label: o.printed ? "Label printed" : "AWB ready", tone: o.printed ? "info" : "attention" };
  if (o.in_cs) return { label: "In CS", tone: "attention" };
  if (o.on_hold) return { label: "On hold", tone: "warning" };
  return { label: "Unfulfilled", tone: "warning" };
}

/* Row-level styles kept out of the component so they aren't re-created 50× per render. */
const ORDER_LINK: React.CSSProperties = {
  background: "none", border: "none", padding: 0, cursor: "pointer",
  color: "var(--p-color-text-emphasis)", font: "inherit", fontWeight: 550, whiteSpace: "nowrap",
};
const CELL_WRAP: React.CSSProperties = { maxWidth: 260 };
const INVOICE_LINK: React.CSSProperties = {
  fontSize: 11, color: "var(--p-color-text-subdued)", textDecoration: "none", whiteSpace: "nowrap",
};
const PRODUCT_LINES: React.CSSProperties = {
  whiteSpace: "pre-line", fontSize: 12, lineHeight: 1.35,
  color: "var(--p-color-text)", overflow: "hidden",
};

const OrderTableRow = memo(function OrderTableRow({
  o, index, selected, cols, busy, creating, menuOpen, profileReady, expanded,
  onDetail, onPrint, onCreateDirect, onCreateOne, setMenuId, menuItems, onToggleExpand,
}: {
  o: OrderRow; index: number; selected: boolean; cols: ColId[];
  busy: boolean; creating: boolean; menuOpen: boolean; profileReady: boolean; expanded: boolean;
  onDetail: (id: number) => void; onPrint: (sid: number) => void;
  onCreateDirect: (id: number) => void; onCreateOne: (id: number) => void;
  setMenuId: (id: number | null) => void; menuItems: (o: OrderRow) => RowMenuItem[];
  onToggleExpand: (id: number) => void;
}) {
  const st = orderStatus(o);
  // PERF: a Polaris <Popover> per row is the single most expensive thing an IndexTable can carry
  // (portal + positioning + document listeners, mounted even while closed). With 50 rows that alone
  // made scrolling stutter, so the Popover is mounted ONLY for the row whose menu is actually open.
  const activator = (
    <Button size="micro" variant="tertiary" disclosure
      onClick={() => setMenuId(menuOpen ? null : o.id)}>More</Button>
  );
  const menu = menuOpen ? (
    <Popover active onClose={() => setMenuId(null)} activator={activator}>
      <ActionList actionRole="menuitem" items={menuItems(o)} />
    </Popover>
  ) : activator;

  const pay = paymentState(o);
  const ful = fulfilmentState(o);

  const CELLS: Record<ColId, React.ReactNode> = {
    order: (
      <div onClick={(e) => e.stopPropagation()}>
        <InlineStack gap="150" blockAlign="center" wrap={false}>
          <button type="button" onClick={() => onDetail(o.id)} style={ORDER_LINK}>
            {o.name ?? `#${o.id}`}
          </button>
          {/* Demo rows sit next to real ones in test mode, so they have to be obvious at a glance. */}
          {o.is_demo && <Badge size="small">Demo</Badge>}
        </InlineStack>
      </div>
    ),
    status: <Badge tone={st.tone}>{st.label}</Badge>,
    payment: <Badge tone={pay.tone} size="small">{pay.label}</Badge>,
    fulfilment: <Badge tone={ful.tone} size="small">{ful.label}</Badge>,
    store: <Text as="span" tone="subdued" variant="bodySm">{o.store_name ?? o.store_domain ?? "—"}</Text>,
    customer: (
      <BlockStack gap="0">
        <Text as="span" variant="bodySm">{o.customer ?? o.address.name ?? "—"}</Text>
        {o.address.phone && <Text as="span" tone="subdued" variant="bodySm">{o.address.phone}</Text>}
      </BlockStack>
    ),
    date: <Text as="span" variant="bodySm" tone="subdued">{whenShort(o.created_at)}</Text>,
    total: (
      <div style={{ textAlign: "right" }}>
        <BlockStack gap="0" inlineAlign="end">
          <Text as="span" variant="bodySm" fontWeight="medium">{money(o.total_price)}</Text>
          {!!o.line_count && (
            <Text as="span" tone="subdued" variant="bodySm">
              {`${o.line_count} item${o.line_count === 1 ? "" : "s"}`}
            </Text>
          )}
        </BlockStack>
      </div>
    ),
    products: (
      /* PERF: the product list is plain text in ONE node instead of one <Text> per line. */
      <div style={CELL_WRAP}>
        <div style={PRODUCT_LINES}>
          {o.items.length === 0
            ? "—"
            : o.items.slice(0, 3).map((it) => `${it.title ?? it.sku ?? "—"} × ${it.quantity ?? 0}`)
              .join("\n") + (o.items.length > 3 ? `\n+${o.items.length - 3} more` : "")}
        </div>
        {o.product_tags && o.product_tags.length > 0 && (
          <div style={{ marginTop: 4 }}>
            <InlineStack gap="050" wrap>
              {o.product_tags.slice(0, 2).map((t) => (
                <Badge key={t} size="small" tone="info">{t}</Badge>
              ))}
              {o.product_tags.length > 2 && (
                <Text as="span" tone="subdued" variant="bodySm">{`+${o.product_tags.length - 2}`}</Text>
              )}
            </InlineStack>
          </div>
        )}
      </div>
    ),
    address: (
      <BlockStack gap="050" inlineAlign="start">
        <Text as="span" variant="bodySm">
          {[o.address.address1, o.address.address2].filter(Boolean).join(", ") || "—"}
        </Text>
        <Text as="span" tone="subdued" variant="bodySm">
          {[o.address.zip, o.address.city, o.address.province].filter(Boolean).join(", ")}
        </Text>
        <Badge tone={addressTone(o.address_status)} size="small">{o.address_status ?? "unvalidated"}</Badge>
      </BlockStack>
    ),
    awb: o.awb ? (
      <div onClick={(e) => e.stopPropagation()}>
        <BlockStack gap="050" inlineAlign="start">
          <Text as="span" variant="bodySm">
            <Text as="span" tone="subdued">{o.courier ? `${o.courier} · ` : ""}</Text>
            {o.tracking_url ? (
              <a href={o.tracking_url} target="_blank" rel="noreferrer">{o.awb}</a>
            ) : (o.awb)}
          </Text>
          <Badge tone={o.printed ? "success" : "attention"} size="small">
            {o.printed ? "Printed" : "Not printed"}
          </Badge>
        </BlockStack>
      </div>
    ) : (
      <Text as="span" tone="subdued">{o.assigned_courier ?? "—"}</Text>
    ),
    invoice: o.invoice_number ? (
      <div onClick={(e) => e.stopPropagation()}>
        {o.invoice_url ? (
          <a href={o.invoice_url} target="_blank" rel="noreferrer" style={INVOICE_LINK}>{o.invoice_number}</a>
        ) : (
          <Text as="span" variant="bodySm">{o.invoice_number}</Text>
        )}
      </div>
    ) : (
      <Text as="span" tone="subdued" variant="bodySm">—</Text>
    ),
  };

  const a = o.address || ({} as OrderRow["address"]);
  return (
    <>
    <IndexTable.Row id={String(o.id)} position={index} selected={selected}
      onClick={() => onToggleExpand(o.id)}>
      {cols.map((c) => <IndexTable.Cell key={c}>{CELLS[c]}</IndexTable.Cell>)}
      <IndexTable.Cell>
        {/* ONE primary action per row + the menu. Void moved into the menu (it's destructive and was
            sitting one pixel from Print), so every row is the same shape instead of a ragged wall. */}
        {st.label === "Cancelled" && !o.shipment_id ? (
          <Text as="span" tone="subdued">—</Text>
        ) : (
          <div onClick={(e) => e.stopPropagation()}
            style={{ display: "flex", justifyContent: "flex-end", gap: 6, flexWrap: "nowrap", whiteSpace: "nowrap" }}>
              {o.shipment_id ? (
                <Button size="micro" loading={busy} onClick={() => onPrint(o.shipment_id!)}>Print</Button>
              ) : (
                <Button size="micro" loading={creating}
                  onClick={() => (profileReady ? onCreateDirect(o.id) : onCreateOne(o.id))}>Create AWB</Button>
              )}
              {menu}
          </div>
        )}
      </IndexTable.Cell>
    </IndexTable.Row>
    {/* Expand pe click (stil AWB Arona): conținutul comenzii + adresa completă, fără fetch — totul e pe rând. */}
    {expanded && (
      <IndexTable.Row id={`${o.id}-detail`} position={index} rowType="child" hideSelectable
        onClick={() => onToggleExpand(o.id)}>
        <IndexTable.Cell colSpan={cols.length + 2}>
          <div onClick={(e) => e.stopPropagation()}
            style={{ display: "flex", gap: 32, padding: "10px 4px 12px", flexWrap: "wrap",
                     background: "var(--p-color-bg-surface-secondary)", borderRadius: 8 }}>
            <div style={{ minWidth: 260, flex: "1 1 300px", paddingLeft: 8 }}>
              <Text as="p" variant="bodySm" tone="subdued" fontWeight="semibold">{t("exp.products")} ({o.line_count})</Text>
              <div style={PRODUCT_LINES}>
                {(o.items || []).map((it, k) => (
                  <div key={k}>{it.quantity ?? 1} × {it.title ?? "—"}{it.sku ? `  (${it.sku})` : ""}</div>
                ))}
              </div>
            </div>
            <div style={{ minWidth: 240, flex: "1 1 260px" }}>
              <Text as="p" variant="bodySm" tone="subdued" fontWeight="semibold">{t("exp.address")}</Text>
              <div style={PRODUCT_LINES}>
                {a?.name && <div>{a.name}</div>}
                {a?.address1 && <div>{a.address1}{a.address2 ? `, ${a.address2}` : ""}</div>}
                <div>{[a?.zip, a?.city].filter(Boolean).join(" ")}{a?.province ? `, ${a.province}` : ""}{a?.country ? `, ${a.country}` : ""}</div>
                {(a?.phone || o.phone) && <div>📞 {a?.phone || o.phone}</div>}
              </div>
            </div>
            <div style={{ minWidth: 200, flex: "1 1 220px" }}>
              <InlineStack gap="200" blockAlign="center">
                <Text as="p" variant="bodySm" tone="subdued" fontWeight="semibold">{t("exp.state")}</Text>
                <Button size="micro" onClick={() => onDetail(o.id)}>{t("exp.edit")}</Button>
              </InlineStack>
              <div style={PRODUCT_LINES}>
                <div>{pay.label} · {ful.label}</div>
                {o.awb && <div>AWB: {o.awb} ({o.courier || "?"})</div>}
                {o.invoice_number && <div>{t("exp.invoice")}: {o.invoice_number}</div>}
                <div>{t("exp.total")}: {o.total_price != null ? o.total_price.toFixed(2) : "—"}</div>
              </div>
            </div>
          </div>
        </IndexTable.Cell>
      </IndexTable.Row>
    )}
    </>
  );
});

export default function Orders() {
  const [data, setData] = useState<OrdersResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [q, setQ] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const [scope, setScope] = useState(() => searchParams.get("scope") || "all");
  const [perPage, setPerPage] = useState(50);
  const [org, setOrg] = useState<OrgResponse | null>(null);
  const [colsOpen, setColsOpen] = useState(false);
  const [cols, setCols] = useState<ColId[]>(() => {
    try {
      const raw = localStorage.getItem(COLS_KEY);
      const parsed = raw ? (JSON.parse(raw) as ColId[]) : null;
      if (Array.isArray(parsed) && parsed.length) return parsed;
    } catch { /* fall through to the defaults */ }
    return DEFAULT_COLS;
  });
  const [lens, setLens] = useState(() => searchParams.get("lens") || "all");
  const [views, setViews] = useState<Record<string, { cols: ColId[]; lens?: string }>>(() => {
    try { return JSON.parse(localStorage.getItem(VIEWS_KEY) || "{}"); } catch { return {}; }
  });
  const [viewName, setViewName] = useState("");
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const toggleExpand = useCallback((id: number) => setExpandedId((p) => (p === id ? null : id)), []);
  const [filters, setFilters] = useState<OrderFilters>({});
  const [sort, setSort] = useState("date_desc");
  const [sbConfigured, setSbConfigured] = useState(false);
  const [productTags, setProductTags] = useState<string[]>([]);
  const [tagSyncBusy, setTagSyncBusy] = useState(false);
  const [, setCmd] = useState("");
  // The shared command console (the floating bubble). Orders owns execution, so it registers its
  // runner here; every answer is echoed back into the console's conversation.
  const bus = useCommandBus();
  const lastCmd = useRef<string>("");
  const echo = useCallback((msg: string, isError = false) => {
    toast(msg, isError);
    bus.log(msg, isError);
  }, [bus]);
  // When a bulk command (`awb-all …`) targets the whole filtered set, its order IDs live here and
  // override the visible-row selection for the create dialog. null = use the checkbox selection.
  const [bulkIds, setBulkIds] = useState<number[] | null>(null);

  const [accounts, setAccounts] = useState<CourierAccount[]>([]);
  const [profiles, setProfiles] = useState<ShipmentProfile[]>([]);
  const [profileId, setProfileId] = useState<string>(""); // "" = custom (raw courier pick)
  const [modalOpen, setModalOpen] = useState(false);
  const [account, setAccount] = useState("");
  const [size, setSize] = useState("A6");
  const [addressId, setAddressId] = useState("");
  const [splitByLocation, setSplitByLocation] = useState(false);
  const [lockerQ, setLockerQ] = useState("");
  const [lockerResults, setLockerResults] = useState<Locker[]>([]);
  const [lockerBusy, setLockerBusy] = useState(false);
  const [chosenLocker, setChosenLocker] = useState<string | null>(null);
  const [showMap, setShowMap] = useState(false);
  const mapIframeRef = useRef<HTMLIFrameElement | null>(null);
  const [detail, setDetail] = useState<OrderDetail | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [timeline, setTimeline] = useState<TimelineEvent[] | null>(null);
  const [invoicing, setInvoicing] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<BulkAwbResponse | null>(null);
  const [busyRow, setBusyRow] = useState<number | null>(null);
  const [creatingId, setCreatingId] = useState<number | null>(null);
  const [showSaveProf, setShowSaveProf] = useState(false);
  const [saveProfName, setSaveProfName] = useState("");
  const [savingProf, setSavingProf] = useState(false);
  // Per-order path: when set, the create modal targets this ONE order and shows override fields.
  const [oneOrderId, setOneOrderId] = useState<number | null>(null);
  const [showOverrides, setShowOverrides] = useState(false);
  const OV_BLANK = { parcels: "", weight: "", length: "", width: "", height: "", content: "", cod: "", declared: "" };
  const [ov, setOv] = useState(OV_BLANK);
  const [ovPerProduct, setOvPerProduct] = useState(false);

  useEffect(() => {
    const t = setTimeout(() => {
      setDebouncedQ(q);
      setPage(1);
    }, 350);
    return () => clearTimeout(t);
  }, [q]);

  useEffect(() => { smartbillStatus().then((r) => setSbConfigured(r.configured)).catch(() => {}); }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await listOrders({ page, per_page: perPage, q: debouncedQ, scope, lens, sort, ...filters }));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to load orders.");
    } finally {
      setLoading(false);
    }
  }, [page, perPage, debouncedQ, scope, lens, sort, filters]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    getOrg().then(setOrg).catch(() => setOrg(null));
  }, []);

  // Product-tag facet: the distinct Shopify product tags seen across orders in scope.
  const loadProductTags = useCallback(() => {
    getProductTags(scope).then((r) => setProductTags(r.tags)).catch(() => setProductTags([]));
  }, [scope]);
  useEffect(() => { loadProductTags(); }, [loadProductTags]);

  const syncProductTags = useCallback(async () => {
    setTagSyncBusy(true);
    try {
      const r = await backfillProductTags(scope);
      toast(r.updated ? `Synced product tags for ${r.updated} items` : "Product tags already up to date");
      loadProductTags();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Sync failed", true);
    } finally {
      setTagSyncBusy(false);
    }
  }, [scope, loadProductTags]);

  // Deep-link entry (e.g. from the Overview dashboard): apply ?scope= / ?lens= on navigation.
  useEffect(() => {
    const sc = searchParams.get("scope");
    const ln = searchParams.get("lens");
    if (sc) setScope(sc);
    if (ln) setLens(ln);
  }, [searchParams]);

  // Bridge to the embedded locker-map widget (/static/locker-map.html): when it's ready,
  // push the courier's lockers; when the user clicks a pin's "Select", set the address_id.
  useEffect(() => {
    function onMsg(e: MessageEvent) {
      const d = (e.data || {}) as { type?: string; locker?: Locker };
      if (d.type === "orderhub-map-ready" && mapIframeRef.current) {
        const acct = accounts.find((a) => a.account_key === account);
        getLockers({ courier: acct?.courier_type, limit: 20000 })
          .then((r) => mapIframeRef.current?.contentWindow?.postMessage(
            { type: "orderhub-lockers", lockers: r.lockers }, "*"))
          .catch(() => {});
      } else if (d.type === "orderhub-locker-selected" && d.locker) {
        setAddressId(String(d.locker.id));
        setChosenLocker(`${d.locker.name} · ${d.locker.city}`);
        setShowMap(false);
      }
    }
    window.addEventListener("message", onMsg);
    return () => window.removeEventListener("message", onMsg);
  }, [accounts, account]);

  useEffect(() => {
    getCouriers()
      .then((r) => {
        const usable = r.accounts.filter((a) => a.has_credentials && a.is_active);
        setAccounts(usable);
        setProfiles(r.profiles);
        // Sticky one-step: restore the last-used profile (or default to the first) so the
        // operator doesn't re-pick courier + parcels + content on every order.
        const remembered = (() => { try { return localStorage.getItem(LAST_PROFILE_KEY) ?? ""; } catch { return ""; } })();
        const chosen = r.profiles.find((p) => String(p.id) === remembered) ?? r.profiles[0];
        if (chosen) {
          setProfileId(String(chosen.id));
          setAccount(chosen.account_key);
          if (chosen.default_label_size) setSize(chosen.default_label_size); // label size from profile
        } else if (usable[0]) {
          setAccount(usable[0].account_key);
        }
      })
      .catch(() => setAccounts([]));
  }, []);

  const orders = data?.orders ?? [];
  const total = data?.total ?? 0;
  const hasNext = page * perPage < total;

  const resourceIds = useMemo(() => orders.map((o) => ({ id: String(o.id) })), [orders]);
  const { selectedResources, allResourcesSelected, handleSelectionChange, clearSelection } =
    useIndexResourceState(resourceIds);
  const selectedIds = selectedResources.map((s) => Number(s));
  const selectedSet = useMemo(() => new Set(selectedResources), [selectedResources]);

  const courierOptions = accounts.map((a) => ({ label: `${a.name} (${a.account_key})`, value: a.account_key }));
  const needsAddressId = /packeta|zasilkovna/i.test(account); // locker/pickup-point id required
  const profileOptions = [
    ...profiles.map((p) => ({ label: p.name, value: String(p.id) })),
    { label: "— Custom courier —", value: "" },
  ];
  const chooseProfile = useCallback((v: string) => {
    setProfileId(v);
    try { localStorage.setItem(LAST_PROFILE_KEY, v); } catch { /* ignore */ }
    const p = profiles.find((x) => String(x.id) === v);
    if (p) {
      setAccount(p.account_key); // keep courier-dependent UI (lockers) in sync
      if (p.default_label_size) setSize(p.default_label_size);
    }
  }, [profiles]);

  const isMultiStore = !!org && org.stores.length > 1;
  const scopeOptions = [
    { label: "This store", value: "store" },
    { label: "All stores", value: "all" },
    ...(org?.groups ?? []).map((g) => ({ label: `Group: ${g}`, value: `group:${g}` })),
    ...(org?.stores ?? []).filter((s) => !s.is_me)
      .map((s) => ({ label: s.name ?? s.domain, value: `store:${s.id}` })),
  ];
  // Keep a deep-linked scope selectable even before the org list has loaded.
  if (!scopeOptions.some((o) => o.value === scope)) {
    scopeOptions.push({ label: scope.startsWith("store:") ? "Selected store" : scope, value: scope });
  }
  const showStoreCol = isMultiStore && scope !== "store";
  // The chosen columns, in the USER'S order (`cols` is an ordered array — the ↑/↓ controls in the
  // column chooser rearrange it). Always-on columns stay pinned first; the Store column drops out
  // when you're looking at a single store.
  const colApplies = (id: ColId) => {
    const c = ALL_COLUMNS.find((x) => x.id === id);
    return !!c && (!c.multiStoreOnly || showStoreCol);
  };
  const alwaysIds = ALL_COLUMNS.filter((c) => c.always).map((c) => c.id);
  const visibleCols: ColId[] = [
    ...alwaysIds.filter(colApplies),
    ...cols.filter((id) => !alwaysIds.includes(id) && colApplies(id)),
  ];
  const headings = [
    ...visibleCols.map((id) => {
      const c = ALL_COLUMNS.find((x) => x.id === id)!;
      return c.end ? { title: c.label, alignment: "end" as const } : { title: c.label };
    }),
    { title: t("col.actions"), alignment: "end" as const },
  ];
  const toggleCol = (id: ColId) => {
    setCols((prev) => {
      const next = prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id];
      try { localStorage.setItem(COLS_KEY, JSON.stringify(next)); } catch { /* quota */ }
      return next;
    });
  };
  // Reorder a chosen column one step up/down (the table renders in `cols` order).
  const moveCol = (id: ColId, dir: -1 | 1) => {
    setCols((prev) => {
      const ordered = prev.filter((x) => !alwaysIds.includes(x));
      const i = ordered.indexOf(id);
      const j = i + dir;
      if (i < 0 || j < 0 || j >= ordered.length) return prev;
      const next = [...ordered];
      [next[i], next[j]] = [next[j], next[i]];
      try { localStorage.setItem(COLS_KEY, JSON.stringify(next)); } catch { /* quota */ }
      return next;
    });
  };
  // Saved VIEWS (named column-set + order + lens), persisted locally alongside the column choice.
  const saveView = () => {
    const name = viewName.trim();
    if (!name) return;
    const next = { ...views, [name]: { cols, lens } };
    setViews(next); setViewName("");
    try { localStorage.setItem(VIEWS_KEY, JSON.stringify(next)); } catch { /* quota */ }
  };
  const applyView = (name: string) => {
    const v = views[name];
    if (!v) return;
    setCols(v.cols.filter((id) => ALL_COLUMNS.some((c) => c.id === id)));
    if (v.lens) { setLens(v.lens); setPage(1); clearSelection(); }
    try { localStorage.setItem(COLS_KEY, JSON.stringify(v.cols)); } catch { /* quota */ }
  };
  const deleteView = (name: string) => {
    const next = { ...views };
    delete next[name];
    setViews(next);
    try { localStorage.setItem(VIEWS_KEY, JSON.stringify(next)); } catch { /* quota */ }
  };

  const lensTabs = [
    { id: "all", content: t("lens.all") },
    { id: "unfulfilled", content: t("lens.unfulfilled") },
    { id: "fulfilled", content: t("lens.fulfilled") },
    { id: "in_transit", content: t("lens.in_transit") },
    { id: "delivered", content: t("lens.delivered") },
    { id: "refused", content: t("lens.refused") },
  ];
  const lensIndex = Math.max(0, lensTabs.findIndex((t) => t.id === lens));

  const doBulkCreate = useCallback(async () => {
    const ids = oneOrderId != null ? [oneOrderId] : (bulkIds ?? selectedIds);
    const single = ids.length === 1;
    if (!account || ids.length === 0) return;
    setSubmitting(true);
    setResult(null);
    try {
      const opts: Record<string, unknown> = { label_size: size };
      if (addressId.trim()) opts.address_id = addressId.trim();
      const profNum = profileId ? Number(profileId) : undefined; // saved profile fills the rest
      // Per-order overrides (single order only) win over the profile in the backend resolver.
      if (single) {
        const n = (v: string) => (v.trim() === "" ? undefined : Number(v));
        const explicit: string[] = []; // fields the user typed → packing must not override them
        if (n(ov.parcels) !== undefined) { opts.parcels_count = n(ov.parcels); explicit.push("parcels_count"); }
        if (n(ov.weight) !== undefined) { opts.total_weight = n(ov.weight); explicit.push("total_weight"); }
        if (n(ov.length) !== undefined) { opts.length = n(ov.length); explicit.push("length"); }
        if (n(ov.width) !== undefined) { opts.width = n(ov.width); explicit.push("width"); }
        if (n(ov.height) !== undefined) { opts.height = n(ov.height); explicit.push("height"); }
        if (n(ov.cod) !== undefined) opts.cod_amount = n(ov.cod);
        if (n(ov.declared) !== undefined) opts.declared_value = n(ov.declared);
        if (ov.content.trim()) opts.content = ov.content.trim();
        if (ovPerProduct) opts.per_product = true;
        if (explicit.length) opts._explicit = explicit;
      }

      let res: BulkAwbResponse;
      if (splitByLocation) {
        // One AWB per fulfillment location, per order — aggregate into one result.
        const created: CreatedAwb[] = [];
        const errors: { order_id: number | string; error: string }[] = [];
        for (const oid of ids) {
          try {
            const r = await splitAwb(oid, account, opts, profNum);
            r.created.forEach((c) =>
              created.push({ order_id: oid, order_name: c.location, awb: c.awb, courier: account }));
            r.errors.forEach((e) => errors.push({ order_id: oid, error: `${e.location ?? ""}: ${e.error}` }));
          } catch (e) {
            errors.push({ order_id: oid, error: e instanceof Error ? e.message : "split failed" });
          }
        }
        res = { success: created.length > 0, created, errors, total: ids.length, pickup: null };
      } else {
        res = await createBulkAwb(ids, account, opts, profNum);
      }

      setResult(res);
      toast(`${res.created.length} AWB created${res.errors.length ? `, ${res.errors.length} failed` : ""}`);
      await load();
      if (res.created.length) {
        setModalOpen(false);
        setOneOrderId(null);
        setBulkIds(null);
        if (oneOrderId == null) clearSelection();
      }
    } catch (e) {
      toast(e instanceof Error ? e.message : "Create failed", true);
    } finally {
      setSubmitting(false);
    }
  }, [account, profileId, oneOrderId, bulkIds, selectedIds, size, addressId, ov, ovPerProduct, splitByLocation, load, clearSelection]);

  // One-click AWB straight from the row when a profile is set (no modal, no customize) — the profile
  // supplies courier + label size + parcels + content. Falls back to the modal for locker couriers.
  const doCreateDirect = useCallback(async (orderId: number) => {
    setCreatingId(orderId);
    try {
      const r = await createBulkAwb([orderId], account, {}, profileId ? Number(profileId) : undefined);
      if (r.created.length) { toast(`AWB ${r.created[0].awb} created`); await load(); }
      else toast(r.errors[0]?.error ?? "Create failed", true);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Create failed", true);
    } finally {
      setCreatingId(null);
    }
  }, [account, profileId, load]);

  // Save what's filled in the create modal (courier + label size + parcels/weight/dims/content) as a
  // reusable profile, then select it — "fill once, it stays".
  const saveAsProfile = useCallback(async () => {
    if (!saveProfName.trim() || !account) { toast("Name and courier are required", true); return; }
    setSavingProf(true);
    try {
      const n = (v: string) => (v.trim() === "" ? null : Number(v));
      const p = await createProfile({
        name: saveProfName.trim(), account_key: account,
        default_parcels: n(ov.parcels) ?? 1, default_weight_kg: n(ov.weight) ?? 1,
        default_length_cm: n(ov.length), default_width_cm: n(ov.width), default_height_cm: n(ov.height),
        default_service_id: null, default_payer: "SENDER", default_packing: null,
        default_label_size: size, content_template: ov.content.trim() || null,
      });
      const r = await getCouriers();
      setProfiles(r.profiles);
      setProfileId(String(p.id));
      try { localStorage.setItem(LAST_PROFILE_KEY, String(p.id)); } catch { /* ignore */ }
      setShowSaveProf(false); setSaveProfName("");
      toast(`Profile "${p.name}" saved`);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Save failed", true);
    } finally {
      setSavingProf(false);
    }
  }, [saveProfName, account, ov, size]);

  const searchLockers = useCallback(async () => {
    const acct = accounts.find((a) => a.account_key === account);
    setLockerBusy(true);
    try {
      const r = await getLockers({ courier: acct?.courier_type, q: lockerQ, limit: 40 });
      setLockerResults(r.lockers);
      if (!r.lockers.length) toast("No lockers found for that search");
    } catch (e) {
      toast(e instanceof Error ? e.message : "Locker search failed", true);
    } finally {
      setLockerBusy(false);
    }
  }, [accounts, account, lockerQ]);

  const doPrint = useCallback(async (shipment_id: number) => {
    setBusyRow(shipment_id);
    try {
      await printLabel(shipment_id, size);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Print failed", true);
    } finally {
      setBusyRow(null);
    }
  }, [size]);

  const doVoid = useCallback(async (shipment_id: number) => {
    setBusyRow(shipment_id);
    try {
      const r = await voidAwb(shipment_id);
      toast(`AWB ${r.voided_awb} cancelled`);
      await load();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Void failed", true);
    } finally {
      setBusyRow(null);
    }
  }, [load]);

  // Update the visible list in place (no full reload → no scroll-jump).
  const removeRows = useCallback((ids: number[]) => {
    setData((d) => d ? { ...d, orders: d.orders.filter((o) => !ids.includes(o.id)),
      total: Math.max(0, d.total - ids.length) } : d);
  }, []);
  const patchRow = useCallback((id: number, patch: Partial<OrderRow>) => {
    setData((d) => d ? { ...d, orders: d.orders.map((o) => o.id === id ? { ...o, ...patch } : o) } : d);
  }, []);
  // Sending to CS: in the Unfulfilled working queue the order drops out; elsewhere it stays,
  // tagged "Sent to CS". Either way — no full reload, so the scroll position is preserved.
  const afterSentToCS = useCallback((ids: number[]) => {
    if (lens === "unfulfilled") removeRows(ids);
    else ids.forEach((id) => patchRow(id, { in_cs: true }));
  }, [lens, removeRows, patchRow]);

  // Per-row overflow menu (a single shared Popover keyed by order id).
  const [menuId, setMenuId] = useState<number | null>(null);
  const [menuBusy, setMenuBusy] = useState(false);
  const rowAction = useCallback(async (fn: () => Promise<unknown>, ok: string, after?: () => void) => {
    setMenuBusy(true);
    try { await fn(); toast(ok); setMenuId(null); after?.(); }
    catch (e) { toast(e instanceof Error ? e.message : "Action failed", true); }
    finally { setMenuBusy(false); }
  }, []);

  const bulkSendToCS = useCallback(async (idsArg?: number[]) => {
    const ids = idsArg ?? [...selectedIds];
    if (ids.length === 0) return;
    let ok = 0;
    for (const id of ids) {
      try { await addToCSQueue(id, "manual"); ok += 1; } catch { /* skip */ }
    }
    afterSentToCS(ids); clearSelection();
    toast(`Sent ${ok} order${ok === 1 ? "" : "s"} to CS backlog`);
  }, [selectedIds, afterSentToCS, clearSelection]);

  // Void the AWB on many orders at once (the `void-all` command's executor). Loops the single-void
  // endpoint; best-effort per shipment.
  const bulkVoid = useCallback(async (shipmentIds: number[]) => {
    if (shipmentIds.length === 0) return;
    let ok = 0; const errs: string[] = [];
    for (const sid of shipmentIds) {
      try { await voidAwb(sid); ok += 1; } catch { errs.push(String(sid)); }
    }
    clearSelection();
    toast(`Voided ${ok} AWB${ok === 1 ? "" : "s"}${errs.length ? `, ${errs.length} failed` : ""}`, errs.length > 0);
    void load();
  }, [clearSelection, load]);

  // Bulk SmartBill invoicing. These are REAL fiscal documents → confirm, skip already-invoiced,
  // then reload so the new invoice numbers show. Runs sequentially to stay under SmartBill's rate.
  const [invoicingBulk, setInvoicingBulk] = useState(false);
  const bulkCreateInvoice = useCallback(async (idsArg?: number[]) => {
    const ids = idsArg ?? orders.filter((o) => selectedSet.has(String(o.id)) && !o.invoice_number).map((o) => o.id);
    const already = idsArg ? 0 : selectedIds.length - ids.length;
    if (ids.length === 0) {
      toast(already ? "All selected orders already have an invoice." : "No orders selected.", true);
      return;
    }
    if (!confirm(`Create ${ids.length} SmartBill invoice${ids.length === 1 ? "" : "s"}?` +
      (already ? ` (${already} already invoiced — skipped.)` : "") +
      "\nThese are real fiscal documents.")) return;
    setInvoicingBulk(true);
    let ok = 0; const errs: string[] = [];
    for (const id of ids) {
      try { await createInvoice(id); ok += 1; }
      catch (e) { errs.push(`#${id}: ${e instanceof ApiError ? e.message : "failed"}`); }
    }
    setInvoicingBulk(false);
    clearSelection();
    toast(`Created ${ok} invoice${ok === 1 ? "" : "s"}${errs.length ? `, ${errs.length} failed` : ""}`, errs.length > 0);
    void load();
  }, [orders, selectedSet, selectedIds, clearSelection, load]);

  // Printable pick sheets (Code128 barcodes) for the selected orders → opens the PDF to print/scan.
  const bulkPickSheet = useCallback(async (idsArg?: number[]) => {
    const ids = idsArg ?? selectedIds;
    if (ids.length === 0) return;
    try {
      const blob = await pickSheet(ids);
      const url = URL.createObjectURL(blob);
      window.open(url, "_blank");
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Couldn't build the pick sheet", true);
    }
  }, [selectedIds]);

  // "Add external AWB" modal (attach a courier AWB created outside the app).
  const [manualFor, setManualFor] = useState<OrderRow | null>(null);
  // "Edit / swap order" modal.
  const [editFor, setEditFor] = useState<OrderRow | null>(null);
  const [noteDraft, setNoteDraft] = useState<string | null>(null);
  const [noteSaving, setNoteSaving] = useState(false);

  const rowMenuItems = useCallback((o: OrderRow) => {
    const items: { content: string; destructive?: boolean; disabled?: boolean; onAction: () => void }[] = [];
    if (!o.shipment_id && orderStatus(o).label !== "Cancelled") {
      items.push({ content: "Add external AWB", disabled: menuBusy,
        onAction: () => { setMenuId(null); setManualFor(o); } });
    }
    if (!o.financial_paid) {
      items.push({ content: "Mark as paid", disabled: menuBusy,
        onAction: () => void rowAction(() => markPaid(o.id), "Marked paid", () => patchRow(o.id, { financial_paid: true })) });
    }
    items.push({ content: "Mark as delivered", disabled: menuBusy,
      onAction: () => void rowAction(() => markDelivered(o.id), "Marked delivered", () => patchRow(o.id, { derived_status: "delivered", last_status: "Livrat" })) });
    if (sbConfigured && o.invoice_number) {
      items.push({ content: "Cancel invoice", destructive: true, disabled: menuBusy,
        onAction: () => {
          setMenuId(null);
          const inv = o.invoice_number;
          if (!confirm(`Cancel invoice ${inv}?\n\nThe invoice is marked cancelled in SmartBill and the order can be invoiced again. Use a storno instead if the customer already has this invoice.`)) return;
          void rowAction(() => cancelInvoice(o.id, "cancel"), "Invoice cancelled", () => void load());
        } });
      items.push({ content: "Storno invoice (credit note)", destructive: true, disabled: menuBusy,
        onAction: () => {
          setMenuId(null);
          const inv = o.invoice_number;
          if (!confirm(`Issue a storno (credit note) reversing invoice ${inv}?\n\nBoth documents stay on record — this is the correct route once the customer has the original.`)) return;
          void rowAction(() => cancelInvoice(o.id, "storno"), "Storno issued", () => void load());
        } });
    }
    if (sbConfigured && !o.invoice_number) {
      items.push({ content: "Create invoice (SmartBill)", disabled: menuBusy,
        onAction: () => void rowAction(() => createInvoice(o.id),
          "Invoice created",
          () => { /* refetch to pull invoice number */ void load(); }) });
    }
    items.push({ content: "Send to CS backlog", disabled: menuBusy,
      onAction: () => void rowAction(() => addToCSQueue(o.id, "manual"), "Sent to CS backlog", () => afterSentToCS([o.id])) });
    if (orderStatus(o).label !== "Cancelled") {
      items.push({ content: "Edit / swap order", disabled: menuBusy,
        onAction: () => { setMenuId(null); setEditFor(o); } });
    }
    if (!o.shipment_id) {
      items.push(o.on_hold
        ? { content: "Release hold", disabled: menuBusy, onAction: () => void rowAction(() => releaseOrder(o.id), "Hold released", () => patchRow(o.id, { on_hold: false })) }
        : { content: "Put on hold", disabled: menuBusy, onAction: () => void rowAction(() => holdOrder(o.id, "manual"), "Put on hold", () => patchRow(o.id, { on_hold: true })) });
    }
    // Void lives here rather than as a row button — it cancels a real shipment with the courier.
    if (o.shipment_id) {
      items.push({ content: "Void AWB", destructive: true, disabled: menuBusy,
        onAction: () => { setMenuId(null); void doVoid(o.shipment_id!); } });
    }
    if (orderStatus(o).label !== "Cancelled") {
      items.push({ content: "Cancel order", destructive: true, disabled: menuBusy,
        onAction: () => { if (confirm(`Cancel order ${o.name ?? o.id}?`)) void rowAction(() => cancelOrderAction(o.id, {}), "Order cancelled", () => removeRows([o.id])); } });
    }
    return items;
  }, [menuBusy, rowAction, patchRow, removeRows, afterSentToCS, sbConfigured, load, doVoid]);


  const loadTimeline = useCallback(async (id: number) => {
    setTimeline(null);
    try {
      setTimeline((await getOrderTimeline(id)).events);
    } catch {
      setTimeline([]); // best-effort; the section just shows "no activity"
    }
  }, []);

  const openDetail = useCallback(async (id: number) => {
    setNoteDraft(null);
    setDetailOpen(true);
    setDetail(null);
    setDetailLoading(true);
    void loadTimeline(id);
    try {
      setDetail(await getOrderDetail(id));
    } catch (e) {
      toast(e instanceof Error ? e.message : "Failed to load order", true);
    } finally {
      setDetailLoading(false);
    }
  }, [loadTimeline]);

  const refreshDetail = useCallback(async () => {
    if (!detail) return;
    setDetailLoading(true);
    void loadTimeline(detail.id);
    try {
      await syncStatuses();
      setDetail(await getOrderDetail(detail.id));
      await load();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Refresh failed", true);
    } finally {
      setDetailLoading(false);
    }
  }, [detail, load, loadTimeline]);

  const invoiceFromDetail = useCallback(async () => {
    if (!detail) return;
    setInvoicing(true);
    try {
      const r = await createInvoice(detail.id);
      const num = `${r.series ?? ""}${r.number ?? ""}`;
      toast(`Invoice ${num} created`);
      setDetail((d) => d ? { ...d, invoice_number: num || null, invoice_url: r.url } : d);
      patchRow(detail.id, { invoice_number: num || null, invoice_url: r.url });
    } catch (e) {
      toast(e instanceof Error ? e.message : "Invoice failed", true);
    } finally { setInvoicing(false); }
  }, [detail, patchRow]);

  const openCreateOne = useCallback((id: number) => {
    setOneOrderId(id);
    setBulkIds(null);
    setResult(null);
    setShowOverrides(false);
    setOv(OV_BLANK);
    setOvPerProduct(false);
    setAddressId("");
    setChosenLocker(null);
    setModalOpen(true);
  }, []);
  // `overrideIds` = an explicit filtered set (from an `awb-all` command); omit for the checkbox selection.
  const openBulkCreate = (overrideIds?: number[]) => {
    setOneOrderId(null);
    setBulkIds(overrideIds ?? null);
    setResult(null);
    setShowOverrides(false);
    setOv(OV_BLANK);
    setOvPerProduct(false);
    setModalOpen(true);
  };
  const closeCreate = () => { setModalOpen(false); setOneOrderId(null); setBulkIds(null); };
  const targetCount = oneOrderId != null ? 1 : (bulkIds ?? selectedIds).length;
  const oneOrder = oneOrderId != null ? orders.find((o) => o.id === oneOrderId) : null;
  const singleProduct = oneOrder != null && oneOrder.line_count <= 1; // hide per-product rounding

  const promotedBulkActions = [
    { content: `Create AWB (${selectedIds.length})`, onAction: openBulkCreate },
    { content: `Send to CS (${selectedIds.length})`, onAction: () => void bulkSendToCS() },
    { content: "Pick sheet", onAction: () => void bulkPickSheet() },
    ...(sbConfigured
      ? [{ content: invoicingBulk ? "Creating invoices…" : `Create invoice (${selectedIds.length})`,
          disabled: invoicingBulk, onAction: () => void bulkCreateInvoice() }]
      : []),
  ];

  const setFilter = (k: keyof OrderFilters, v: string | number | undefined) => {
    setFilters((p) => {
      const n = { ...p };
      if (v === undefined || v === "") delete n[k];
      else (n as Record<string, unknown>)[k] = v;
      return n;
    });
    setPage(1); clearSelection();
  };
  const choiceFilter = (key: keyof OrderFilters, choices: { label: string; value: string }[]) => (
    <ChoiceList title="" titleHidden choices={choices}
      selected={filters[key] ? [String(filters[key])] : []}
      onChange={(v) => setFilter(key, v[0])} />
  );
  const textFilter = (key: keyof OrderFilters, placeholder: string) => (
    <TextField label={placeholder} labelHidden autoComplete="off" placeholder={placeholder}
      value={String(filters[key] ?? "")} onChange={(v) => setFilter(key, v)} />
  );
  const rangeFilter = (minK: keyof OrderFilters, maxK: keyof OrderFilters) => (
    <InlineStack gap="200">
      <TextField label="Min" type="number" autoComplete="off" min={0}
        value={String(filters[minK] ?? "")} onChange={(v) => setFilter(minK, v ? Number(v) : undefined)} />
      <TextField label="Max" type="number" autoComplete="off" min={0}
        value={String(filters[maxK] ?? "")} onChange={(v) => setFilter(maxK, v ? Number(v) : undefined)} />
    </InlineStack>
  );
  const dateFilter = (
    <InlineStack gap="200">
      <TextField label="From" type="date" autoComplete="off"
        value={String(filters.date_from ?? "")} onChange={(v) => setFilter("date_from", v)} />
      <TextField label="To" type="date" autoComplete="off"
        value={String(filters.date_to ?? "")} onChange={(v) => setFilter("date_to", v)} />
    </InlineStack>
  );
  const filterDefs = [
    { key: "payment", label: "Payment", pinned: true, filter: choiceFilter("payment", CH_PAYMENT) },
    { key: "delivery", label: "Delivery", pinned: true, filter: choiceFilter("delivery", CH_DELIVERY) },
    { key: "printed", label: "Printed", pinned: true, filter: choiceFilter("printed", CH_PRINTED) },
    { key: "invoiced", label: "Invoice", pinned: true, filter: choiceFilter("invoiced", CH_INVOICED) },
    { key: "order_status", label: "Order status", filter: choiceFilter("order_status", CH_ORDER_STATUS) },
    { key: "delivery_status", label: "Delivery status", filter: choiceFilter("delivery_status", CH_DELIVERY_STATUS) },
    { key: "address_status", label: "Address", filter: choiceFilter("address_status", CH_ADDR_STATUS) },
    { key: "product", label: "Product / SKU", filter: textFilter("product", "SKU or product name") },
    { key: "tag", label: "Tag", filter: textFilter("tag", "Order tag") },
    { key: "product_tag", label: "Product tag", filter: (
      <BlockStack gap="200">
        {productTags.length > 0 ? (
          <Select label="Product tag" labelHidden
            options={[{ label: "Any product tag", value: "" },
              ...productTags.map((t) => ({ label: t, value: t }))]}
            value={String(filters.product_tag ?? "")}
            onChange={(v) => setFilter("product_tag", v || undefined)} />
        ) : (
          <Text as="span" tone="subdued">No product tags synced yet — sync to filter by them.</Text>
        )}
        <Button variant="plain" size="slim" loading={tagSyncBusy} onClick={syncProductTags}>
          Sync product tags from Shopify
        </Button>
      </BlockStack>
    ) },
    { key: "province", label: "County (județ)", filter: textFilter("province", "Județ") },
    { key: "city", label: "City", filter: textFilter("city", "City") },
    { key: "courier", label: "Courier", filter: textFilter("courier", "DPD, Sameday…") },
    { key: "qty", label: "Quantity", filter: rangeFilter("qty_min", "qty_max") },
    { key: "total", label: "Order value", filter: rangeFilter("total_min", "total_max") },
    { key: "date", label: "Date", filter: dateFilter },
  ];
  const applied: { key: string; label: string; onRemove: () => void }[] = [];
  const addApplied = (key: string, label: string, keys: (keyof OrderFilters)[]) =>
    applied.push({ key, label, onRemove: () => keys.forEach((k) => setFilter(k, undefined)) });
  if (filters.payment) addApplied("payment", `Payment: ${_labelOf(CH_PAYMENT, String(filters.payment))}`, ["payment"]);
  if (filters.delivery) addApplied("delivery", `Delivery: ${_labelOf(CH_DELIVERY, String(filters.delivery))}`, ["delivery"]);
  if (filters.printed) addApplied("printed", _labelOf(CH_PRINTED, String(filters.printed)), ["printed"]);
  if (filters.invoiced) addApplied("invoiced", _labelOf(CH_INVOICED, String(filters.invoiced)), ["invoiced"]);
  if (filters.order_status) addApplied("order_status", `Status: ${_labelOf(CH_ORDER_STATUS, String(filters.order_status))}`, ["order_status"]);
  if (filters.delivery_status) addApplied("delivery_status", `Delivery: ${_labelOf(CH_DELIVERY_STATUS, String(filters.delivery_status))}`, ["delivery_status"]);
  if (filters.address_status) addApplied("address_status", `Address: ${_labelOf(CH_ADDR_STATUS, String(filters.address_status))}`, ["address_status"]);
  if (filters.product) addApplied("product", `Product: ${filters.product}`, ["product"]);
  if (filters.tag) addApplied("tag", `Tag: ${filters.tag}`, ["tag"]);
  if (filters.product_tag) addApplied("product_tag", `Product tag: ${filters.product_tag}`, ["product_tag"]);
  if (filters.province) addApplied("province", `County: ${filters.province}`, ["province"]);
  if (filters.city) addApplied("city", `City: ${filters.city}`, ["city"]);
  if (filters.courier) addApplied("courier", `Courier: ${filters.courier}`, ["courier"]);
  if (filters.qty_min != null || filters.qty_max != null)
    addApplied("qty", `Qty ${filters.qty_min ?? 0}–${filters.qty_max ?? "∞"}`, ["qty_min", "qty_max"]);
  if (filters.total_min != null || filters.total_max != null)
    addApplied("total", `Value ${filters.total_min ?? 0}–${filters.total_max ?? "∞"}`, ["total_min", "total_max"]);
  if (filters.date_from || filters.date_to)
    addApplied("date", `Date ${filters.date_from ?? "…"} → ${filters.date_to ?? "…"}`, ["date_from", "date_to"]);

  // Match a typed order token (GT1001, #1001, 1001) against the orders on the current page.
  const findLoadedOrder = (tok: string): OrderRow | null => {
    const t = tok.replace(/^#/, "").toLowerCase();
    const digits = t.replace(/\D/g, "");
    return (
      orders.find((o) => {
        const n = (o.name || "").replace(/^#/, "").toLowerCase();
        return n === t || n.endsWith(t) || (digits.length >= 3 && n.replace(/\D/g, "") === digits);
      }) || null
    );
  };

  // Bulk mutation verbs (`awb-all`, `void-all`, `cs-all`, `invoice-all`, `print-all`): resolve the WHOLE
  // filtered set (fresh fetch, not just the visible page) → drive the confirm-gated bulk action on it.
  const runBulk = async (verb: string, filterText: string) => {
    const { filters: parsed } = parseCmdFilters(filterText);
    const merged: OrderFilters = { ...filters, ...parsed };
    setFilters(merged); setPage(1); clearSelection();
    let rows: OrderRow[] = []; let tot = 0;
    try {
      const res = await listOrders({ ...merged, scope, lens, per_page: 200 });
      rows = res.orders; tot = res.total;
    } catch (e) { setCmd(""); echo(e instanceof Error ? e.message : "Query failed", true); return; }
    setCmd("");
    if (!rows.length) { echo("No orders match that filter", true); return; }
    const ids = rows.map((o) => o.id);
    const cap = tot > rows.length ? ` (first ${rows.length} of ${tot} — narrow the filter to cover all)` : "";
    if (verb === "awb") {
      openBulkCreate(ids);
      echo(`Create AWB for ${ids.length} order${ids.length === 1 ? "" : "s"}${cap} — pick courier & confirm`);
    } else if (verb === "print") {
      await bulkPickSheet(ids); echo(`Pick sheet for ${ids.length} order${ids.length === 1 ? "" : "s"}${cap}`);
    } else if (verb === "cs") {
      if (!confirm(`Send ${ids.length} order${ids.length === 1 ? "" : "s"} to CS backlog?${cap}`)) return;
      await bulkSendToCS(ids);
    } else if (verb === "invoice") {
      const inv = rows.filter((o) => !o.invoice_number).map((o) => o.id);
      if (!inv.length) { echo("All matching orders already have an invoice.", true); return; }
      await bulkCreateInvoice(inv);  // has its own fiscal-document confirm
    } else if (verb === "void") {
      const sids = rows.filter((o) => o.shipment_id && o.awb).map((o) => o.shipment_id as number);
      if (!sids.length) { echo("No AWBs to void in that set.", true); return; }
      if (!confirm(`Void the AWB on ${sids.length} order${sids.length === 1 ? "" : "s"}?${cap}\nThis cancels the shipment.`)) return;
      await bulkVoid(sids);
    }
  };

  const runCommand = (raw: string) => {
    const text = raw.trim();
    if (!text) return;
    lastCmd.current = text;
    // 0) bulk mutation verb → act on the whole filtered set.
    const bulkM = text.toLowerCase().match(/^(awb|void|cs|invoice|print)-all\b/);
    if (bulkM) { void runBulk(bulkM[1], text.replace(/^\s*\S+\s*/, "")); return; }
    // 1) key:value tokens + bare dates → faceted filters.
    const { filters: parsed, applied: appliedKV, rest } = parseCmdFilters(text);
    (Object.keys(parsed) as (keyof OrderFilters)[]).forEach((k) => setFilter(k, parsed[k] as string | number));
    const low = rest.toLowerCase();

    // 2) navigation words.
    for (const key of Object.keys(CMD_NAV)) {
      if (low === key || low.startsWith(key + " ") || (low.startsWith(key) && key.length >= 5)) {
        navigate(CMD_NAV[key]); echo(`Go to ${key}`); setCmd(""); return;
      }
    }
    // 3) lens words (whole phrase).
    if (CMD_LENS[low]) {
      setLens(CMD_LENS[low]); setPage(1); clearSelection();
      echo(appliedKV.length ? `Lens ${CMD_LENS[low]} · ${appliedKV.join(", ")}` : `Lens: ${CMD_LENS[low]}`);
      setCmd(""); return;
    }
    // 4) "addr <order>" / "schimba adresa <order>" → open the order (address editor is in the detail panel).
    const addrM = low.match(/^(?:schimba\s+)?(?:addr|address|adresa)\s+(\S+)/);
    if (addrM) {
      const o = findLoadedOrder(addrM[1]);
      if (o) { void openDetail(o.id); echo(`Open ${o.name} — edit the address`); }
      else { setQ(addrM[1]); setPage(1); echo(`${addrM[1]} not on this page — searching`, true); }
      setCmd(""); return;
    }
    // 5) "awb <order> [courier]" → open the (confirm-gated) create dialog.
    const awbM = low.match(/^(?:make\s+)?awb\s+(\S+)(?:\s+(\S+))?/);
    if (awbM) {
      const o = findLoadedOrder(awbM[1]);
      if (o) {
        if (awbM[2]) {
          const acc = accounts.find((a) => a.account_key.toLowerCase() === awbM[2]
            || (a.name || "").toLowerCase().includes(awbM[2]!) || (a.courier_type || "").toLowerCase().includes(awbM[2]!));
          if (acc) setAccount(acc.account_key);
        }
        openCreateOne(o.id);
        echo(`Create AWB for ${o.name}${awbM[2] ? ` (${awbM[2]})` : ""} — confirm in the dialog`);
      } else { setQ(awbM[1]); setPage(1); echo(`${awbM[1]} not on this page — searching; run again when visible`, true); }
      setCmd(""); return;
    }
    // 5) "open/find/show <order>" or a bare token → search.
    const openM = low.match(/^(?:open|find|show|order)\s+(\S+)/);
    const term = openM ? openM[1] : rest;
    if (term) { setQ(term); setPage(1); }
    echo(appliedKV.length ? (term ? `Search “${term}” · ${appliedKV.join(", ")}` : `Filters: ${appliedKV.join(", ")}`) : `Search: ${term}`);
    setCmd("");
  };

  // Keep the console's runner pointing at THIS page's live closure, so a command typed from the
  // bubble while Orders is on screen executes in place (filters, dialogs, selection) instead of
  // bouncing through the URL.
  const runRef = useRef(runCommand);
  runRef.current = runCommand;
  useEffect(() => {
    bus.registerRunner((t) => runRef.current(t));
    return () => bus.registerRunner(null);
  }, [bus]);

  // A command handed over from another page arrives as ?cmd=… — run it once, then drop it from the
  // URL so a refresh doesn't re-fire a bulk action.
  useEffect(() => {
    const handed = searchParams.get("cmd");
    if (!handed || handed === lastCmd.current) return;
    runRef.current(handed);
    navigate("/app/orders", { replace: true });
  }, [searchParams, navigate]);

  return (
    <Page fullWidth title="Orders" subtitle={total ? `${total} orders` : "Your orders and their AWBs."}>
      <Card padding="0">
        {/* Lens tabs + all view controls on one line (tabs grow to the left, controls hug the right). */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between",
          gap: 12, flexWrap: "wrap", padding: "4px 12px", borderBottom: "1px solid var(--p-color-border)" }}>
          <div style={{ flex: "1 1 auto", minWidth: 0 }}>
            <Tabs
              tabs={lensTabs}
              selected={lensIndex}
              onSelect={(i) => { setLens(lensTabs[i].id); setPage(1); clearSelection(); }}
            />
          </div>
          <InlineStack gap="200" blockAlign="center" wrap={false}>
            {isMultiStore && (
              <Select label="Store" labelInline options={scopeOptions} value={scope}
                onChange={(v) => { setScope(v); setPage(1); clearSelection(); }} />
            )}
            <Select label="Sort" labelInline options={SORT_OPTIONS} value={sort}
              onChange={(v) => { setSort(v); setPage(1); }} />
            <Select label="Show" labelInline options={PER_PAGE_OPTIONS} value={String(perPage)}
              onChange={(v) => { setPerPage(Number(v)); setPage(1); }} />
            <Popover
              active={colsOpen}
              onClose={() => setColsOpen(false)}
              activator={
                <Button disclosure onClick={() => setColsOpen(!colsOpen)}>
                  {`${t("cols.button")} (${visibleCols.length})`}
                </Button>
              }
            >
              <Box padding="300" minWidth="230px">
                <BlockStack gap="200">
                  <Text as="p" variant="bodySm" tone="subdued">{t("cols.view")}</Text>
                  <ButtonGroup variant="segmented">
                    {COL_PRESETS.map((p) => {
                      const active = p.cols.length === visibleCols.length
                        && p.cols.every((c) => visibleCols.includes(c));
                      return (
                        <Button key={p.id} size="slim" pressed={active} onClick={() => {
                          setCols(p.cols);
                          try { localStorage.setItem(COLS_KEY, JSON.stringify(p.cols)); } catch { /* quota */ }
                        }}>{p.label}</Button>
                      );
                    })}
                  </ButtonGroup>
                  {Object.keys(views).length > 0 && (
                    <>
                      <Text as="p" variant="bodySm" tone="subdued">{t("views.saved")}</Text>
                      {Object.keys(views).map((name) => (
                        <InlineStack key={name} gap="100" blockAlign="center" wrap={false}>
                          <div style={{ flex: 1 }}>
                            <Button size="slim" fullWidth textAlign="start"
                              onClick={() => applyView(name)}>{name}</Button>
                          </div>
                          <Button size="micro" variant="tertiary" tone="critical"
                            onClick={() => deleteView(name)}>✕</Button>
                        </InlineStack>
                      ))}
                    </>
                  )}
                  <InlineStack gap="100" blockAlign="center" wrap={false}>
                    <div style={{ flex: 1 }}>
                      <TextField label="Save view" labelHidden placeholder={t("views.name")} autoComplete="off"
                        value={viewName} onChange={setViewName} />
                    </div>
                    <Button size="slim" onClick={saveView} disabled={!viewName.trim()}>{t("views.save")}</Button>
                  </InlineStack>
                  <Divider />
                  <Text as="p" variant="bodySm" tone="subdued">{t("cols.show")}</Text>
                  {/* Checked columns in the USER'S order (with ↑/↓), then the unchecked ones. */}
                  {[
                    ...ALL_COLUMNS.filter((c) => c.always),
                    ...cols.map((id) => ALL_COLUMNS.find((c) => c.id === id))
                      .filter((c): c is (typeof ALL_COLUMNS)[number] => !!c && !c.always),
                    ...ALL_COLUMNS.filter((c) => !c.always && !cols.includes(c.id)),
                  ].filter((c) => !c.multiStoreOnly || showStoreCol).map((c) => {
                    const checked = c.always || cols.includes(c.id);
                    return (
                      <InlineStack key={c.id} gap="100" blockAlign="center" wrap={false}>
                        <div style={{ flex: 1 }}>
                          <Checkbox label={c.label} checked={checked} disabled={c.always}
                            onChange={() => toggleCol(c.id)} />
                        </div>
                        {checked && !c.always && (
                          <ButtonGroup variant="segmented">
                            <Button size="micro" onClick={() => moveCol(c.id, -1)}>↑</Button>
                            <Button size="micro" onClick={() => moveCol(c.id, 1)}>↓</Button>
                          </ButtonGroup>
                        )}
                      </InlineStack>
                    );
                  })}

                </BlockStack>
              </Box>
            </Popover>
          </InlineStack>
        </div>
        <div style={{ padding: "12px" }}>
          {/* No command bar here any more: the ⌘K console (bottom-right, on every page) is the one
              place commands live, so Orders opens on its filters instead of a wall of syntax. */}
          <Filters
            queryValue={q}
            queryPlaceholder="Search by order #, phone or city"
            onQueryChange={setQ}
            onQueryClear={() => setQ("")}
            onClearAll={() => { setFilters({}); setPage(1); clearSelection(); }}
            filters={filterDefs}
            appliedFilters={applied}
          />
        </div>

        {error && (
          <div style={{ padding: "0 12px 12px" }}>
            <Banner tone="critical" title="Couldn't load orders" onDismiss={() => setError(null)}>
              <p>{error}</p>
            </Banner>
          </div>
        )}

        {result && (
          <div style={{ padding: "0 12px 12px" }}>
            <Banner
              tone={result.errors.length ? "warning" : "success"}
              title={`${result.created.length} AWB created${result.errors.length ? `, ${result.errors.length} failed` : ""}`}
              onDismiss={() => setResult(null)}
            >
              {result.pickup?.requested && <p>Courier pickup requested.</p>}
              {result.errors.slice(0, 5).map((e) => (
                <p key={String(e.order_id)}>#{e.order_id}: {e.error}</p>
              ))}
            </Banner>
          </div>
        )}

        {loading ? (
          <div style={{ padding: "12px" }}>
            <SkeletonBodyText lines={8} />
          </div>
        ) : orders.length === 0 ? (
          <EmptyState
            heading={debouncedQ ? "No matching orders" : "No orders yet"}
            image="https://cdn.shopify.com/s/files/1/0757/9955/files/empty-state.svg"
          >
            <p>
              {debouncedQ
                ? "Try a different search."
                : "Orders sync automatically from Shopify and appear here within seconds."}
            </p>
          </EmptyState>
        ) : (
          <IndexTable
            resourceName={{ singular: "order", plural: "orders" }}
            itemCount={orders.length}
            selectedItemsCount={allResourcesSelected ? "All" : selectedIds.length}
            onSelectionChange={handleSelectionChange}
            promotedBulkActions={promotedBulkActions}
            headings={headings as [{ title: string }, ...{ title: string }[]]}
          >
            {orders.map((o: OrderRow, i) => (
              <OrderTableRow
                key={o.id}
                o={o}
                index={i}
                selected={selectedSet.has(String(o.id))}
                cols={visibleCols}
                busy={!!o.shipment_id && busyRow === o.shipment_id}
                creating={creatingId === o.id}
                menuOpen={menuId === o.id}
                profileReady={!!profileId && !needsAddressId}
                expanded={expandedId === o.id}
                onToggleExpand={toggleExpand}
                onDetail={openDetail}
                onPrint={doPrint}
                onCreateDirect={doCreateDirect}
                onCreateOne={openCreateOne}
                setMenuId={setMenuId}
                menuItems={rowMenuItems}
              />
            ))}
          </IndexTable>
        )}

        {(hasNext || page > 1) && (
          <div style={{ display: "flex", justifyContent: "center", padding: "12px" }}>
            <Pagination
              hasPrevious={page > 1}
              onPrevious={() => setPage((p) => Math.max(1, p - 1))}
              hasNext={hasNext}
              onNext={() => setPage((p) => p + 1)}
              label={`Page ${page}`}
            />
          </div>
        )}
      </Card>

      <Modal
        open={modalOpen}
        onClose={closeCreate}
        title={oneOrderId != null
          ? "Create AWB · this order"
          : `Create AWB for ${targetCount} order${targetCount === 1 ? "" : "s"}`}
        primaryAction={{ content: "Create AWB", onAction: doBulkCreate, loading: submitting, disabled: !account || targetCount === 0 }}
        secondaryActions={[{ content: "Cancel", onAction: closeCreate }]}
      >
        <Modal.Section>
          <BlockStack gap="400">
            {accounts.length === 0 ? (
              <Banner tone="warning" title="No courier configured">
                <p>Add a courier account with credentials in Settings first.</p>
              </Banner>
            ) : (
              <>
                {profiles.length > 0 && (
                  <Select
                    label="Shipment profile"
                    options={profileOptions}
                    value={profileId}
                    onChange={chooseProfile}
                    helpText="Fills courier + parcels + weight + content in one pick. Your choice is remembered."
                  />
                )}
                {(profiles.length === 0 || profileId === "") && (
                  <Select label="Courier" options={courierOptions} value={account} onChange={setAccount} />
                )}
                <Select label="Label size" options={SIZE_OPTIONS} value={size} onChange={setSize} />
                <TextField
                  label={needsAddressId ? "Pickup point / locker ID (required)" : "Pickup point / locker ID (optional)"}
                  value={addressId}
                  onChange={(v) => { setAddressId(v); setChosenLocker(null); }}
                  autoComplete="off"
                  helpText="Leave empty for home delivery. For locker/pickup-point delivery, enter the point ID — or search below."
                />
                <BlockStack gap="150">
                  <InlineStack gap="200" blockAlign="end">
                    <div style={{ flex: 1 }}>
                      <TextField
                        label="Find a locker"
                        value={lockerQ}
                        onChange={setLockerQ}
                        autoComplete="off"
                        placeholder="Search by city or name (e.g. Cluj, easybox OMV)"
                      />
                    </div>
                    <Button onClick={searchLockers} loading={lockerBusy}>Search</Button>
                    <Button onClick={() => setShowMap((v) => !v)}>{showMap ? "Hide map" : "Map"}</Button>
                  </InlineStack>
                  {showMap && (
                    <iframe
                      ref={mapIframeRef}
                      src="/static/locker-map.html"
                      title="Locker map"
                      style={{ width: "100%", height: "360px", border: "1px solid #e1e3e5", borderRadius: 8 }}
                    />
                  )}
                  {lockerResults.length > 0 && (
                    <div style={{ maxHeight: 180, overflowY: "auto", border: "1px solid #e1e3e5", borderRadius: 8 }}>
                      {lockerResults.map((l) => (
                        <button
                          key={`${l.courier}-${l.id}`}
                          type="button"
                          onClick={() => { setAddressId(l.id); setChosenLocker(`${l.name} · ${l.city}`); }}
                          style={{
                            display: "block", width: "100%", textAlign: "left", padding: "6px 10px",
                            background: addressId === l.id ? "#f1f8f5" : "transparent",
                            border: "none", borderBottom: "1px solid #f1f1f1", cursor: "pointer",
                          }}
                        >
                          <Text as="span" variant="bodySm">
                            <b>{l.name}</b> · {l.city} · <span style={{ opacity: 0.6 }}>{l.courier} #{l.id}</span>
                          </Text>
                        </button>
                      ))}
                    </div>
                  )}
                  {chosenLocker && (
                    <Text as="span" tone="success" variant="bodySm">Selected locker: {chosenLocker}</Text>
                  )}
                </BlockStack>
                {targetCount === 1 && (
                  <BlockStack gap="200">
                    <Button
                      variant="plain"
                      disclosure={showOverrides ? "up" : "down"}
                      onClick={() => setShowOverrides((v) => !v)}
                    >
                      {showOverrides ? "Hide per-order overrides" : "Customize this shipment"}
                    </Button>
                    {showOverrides && (
                      <BlockStack gap="300">
                        <Text as="p" tone="subdued" variant="bodySm">
                          Empty = use the profile / order default. Applies to this order only.
                        </Text>
                        <InlineStack gap="300">
                          <div style={{ flex: 1 }}><TextField label="Parcels" type="number" value={ov.parcels} onChange={(v) => setOv((o) => ({ ...o, parcels: v }))} autoComplete="off" placeholder="profile" /></div>
                          <div style={{ flex: 1 }}><TextField label="Weight (kg)" type="number" value={ov.weight} onChange={(v) => setOv((o) => ({ ...o, weight: v }))} autoComplete="off" placeholder="profile" /></div>
                          <div style={{ flex: 1 }}><TextField label="COD (RON)" type="number" value={ov.cod} onChange={(v) => setOv((o) => ({ ...o, cod: v }))} autoComplete="off" placeholder="order total" /></div>
                        </InlineStack>
                        <InlineStack gap="300">
                          <div style={{ flex: 1 }}><TextField label="Length (cm)" type="number" value={ov.length} onChange={(v) => setOv((o) => ({ ...o, length: v }))} autoComplete="off" /></div>
                          <div style={{ flex: 1 }}><TextField label="Width (cm)" type="number" value={ov.width} onChange={(v) => setOv((o) => ({ ...o, width: v }))} autoComplete="off" /></div>
                          <div style={{ flex: 1 }}><TextField label="Height (cm)" type="number" value={ov.height} onChange={(v) => setOv((o) => ({ ...o, height: v }))} autoComplete="off" /></div>
                          <div style={{ flex: 1 }}><TextField label="Declared (RON)" type="number" value={ov.declared} onChange={(v) => setOv((o) => ({ ...o, declared: v }))} autoComplete="off" /></div>
                        </InlineStack>
                        <TextField
                          label="AWB content"
                          value={ov.content}
                          onChange={(v) => setOv((o) => ({ ...o, content: v }))}
                          autoComplete="off"
                          placeholder="profile template"
                          helpText="Overrides the profile's content for this label only."
                        />
                        {!singleProduct && (
                          <Checkbox
                            label="Round parcels per product"
                            checked={ovPerProduct}
                            onChange={setOvPerProduct}
                            helpText="Each product rounds up into its own parcel(s) instead of sharing. Only affects the auto parcel count."
                          />
                        )}
                        <Checkbox
                          label="Split across fulfillment locations"
                          helpText="For an order shipping from more than one location, create one AWB per location. Single-location orders get one AWB as usual."
                          checked={splitByLocation}
                          onChange={setSplitByLocation}
                        />
                      </BlockStack>
                    )}
                  </BlockStack>
                )}
                {targetCount !== 1 && (
                  <Checkbox
                    label="Split across fulfillment locations"
                    helpText="For orders that ship from more than one location, create one AWB per location."
                    checked={splitByLocation}
                    onChange={setSplitByLocation}
                  />
                )}
                <Text as="p" tone="subdued" variant="bodySm">
                  A courier pickup is requested automatically where the courier needs it (DPD, FAN).
                </Text>
                <div>
                  <Button variant="plain" onClick={() => setShowSaveProf((v) => !v)}>
                    {showSaveProf ? "Cancel" : "Save these settings as a profile"}
                  </Button>
                  {showSaveProf && (
                    <InlineStack gap="200" blockAlign="end">
                      <div style={{ flex: 1 }}>
                        <TextField label="Profile name" value={saveProfName} onChange={setSaveProfName}
                          autoComplete="off" placeholder="e.g. DPD home · A6" />
                      </div>
                      <Button variant="primary" onClick={saveAsProfile} loading={savingProf}>Save profile</Button>
                    </InlineStack>
                  )}
                </div>
              </>
            )}
          </BlockStack>
        </Modal.Section>
      </Modal>

      <Modal
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        title={detail ? `Order ${detail.name ?? `#${detail.id}`}` : "Order"}
        secondaryActions={[
          { content: "Refresh status", onAction: refreshDetail, loading: detailLoading },
          { content: "Close", onAction: () => setDetailOpen(false) },
        ]}
      >
        <Modal.Section>
          {detailLoading && !detail ? (
            <SkeletonBodyText lines={8} />
          ) : detail ? (
            <BlockStack gap="400">
              <InlineStack gap="300" blockAlign="center">
                {(() => {
                  const st = orderStatus({
                    derived_status: detail.derived_status,
                    processing_status: detail.processing_status,
                    last_status: detail.shipments[0]?.last_status ?? null,
                    awb: detail.shipments[0]?.awb ?? null,
                  } as OrderRow);
                  return <Badge tone={st.tone}>{st.label}</Badge>;
                })()}
                <Badge tone={detail.financial_paid ? "success" : "attention"}>
                  {detail.financial_paid ? "Paid" : "COD / unpaid"}
                </Badge>
                <Text as="span" tone="subdued">{money(detail.total_price)}</Text>
                {detail.store_name && <Text as="span" tone="subdued">· {detail.store_name}</Text>}
                {detail.invoice_number ? (
                  <InlineStack gap="100" blockAlign="center">
                    <Badge tone="success">{`Invoice ${detail.invoice_number}`}</Badge>
                    {detail.invoice_url && <a href={detail.invoice_url} target="_blank" rel="noreferrer">PDF ↗</a>}
                  </InlineStack>
                ) : (sbConfigured && !detail.cancelled_at && (
                  <Button size="micro" onClick={() => void invoiceFromDetail()} loading={invoicing}>
                    Create invoice
                  </Button>
                ))}
              </InlineStack>

              <BlockStack gap="150">
                <Text as="h3" variant="headingSm">Shipping address</Text>
                <AddressEditor
                  orderId={detail.id}
                  compact
                  initial={{
                    name: detail.address.name ?? "", phone: detail.address.phone ?? "",
                    address1: detail.address.address1 ?? "", address2: detail.address.address2 ?? "",
                    city: detail.address.city ?? "", zip: detail.address.zip ?? "",
                    province: detail.address.province ?? "", country: detail.address.country ?? "",
                  }}
                  onSaved={(r) => {
                    setDetail((d) => d ? { ...d, address: { ...d.address, status: r.address_status } } : d);
                    void loadTimeline(detail.id); // the edit lands on the timeline
                  }}
                />
                {detail.address.email && <Text as="p" tone="subdued">{detail.address.email}</Text>}
              </BlockStack>

              <BlockStack gap="100">
                <InlineStack align="space-between" blockAlign="center">
                  <Text as="h3" variant="headingSm">Items</Text>
                  {!detail.cancelled_at && (
                    <Button size="slim" onClick={() => {
                      const row = orders.find((o) => o.id === detail.id);
                      setDetailOpen(false);
                      setEditFor(row ?? ({ id: detail.id, name: detail.name } as OrderRow));
                    }}>Edit / swap products</Button>
                  )}
                </InlineStack>
                {detail.line_items.length === 0
                  ? <Text as="p" tone="subdued">—</Text>
                  : detail.line_items.map((li, i) => (
                    <Text as="p" key={i}>
                      {li.quantity} × {li.title ?? li.sku ?? "item"}{li.sku ? ` (${li.sku})` : ""}
                    </Text>
                  ))}
              </BlockStack>

              <BlockStack gap="200">
                <Text as="h3" variant="headingSm">Shipments</Text>
                {detail.shipments.length === 0 ? (
                  <Text as="p" tone="subdued">No AWB yet.</Text>
                ) : detail.shipments.map((s) => (
                  <div key={s.id} style={{ border: "1px solid #e1e3e5", borderRadius: 8, padding: "8px 10px" }}>
                    <InlineStack align="space-between" blockAlign="center" gap="200">
                      <BlockStack gap="050">
                        <InlineStack gap="200" blockAlign="center">
                          <Text as="span" fontWeight="semibold">{s.courier} · {s.awb}</Text>
                          {s.location && <Badge>{s.location}</Badge>}
                          <Badge tone={s.printed ? "success" : "attention"} size="small">
                            {s.printed ? "Printed" : "Not printed"}
                          </Badge>
                        </InlineStack>
                        <Text as="span" tone="subdued">
                          {s.last_status ?? "—"}
                          {s.last_status_at ? ` · ${new Date(s.last_status_at).toLocaleString()}` : ""}
                        </Text>
                      </BlockStack>
                      <ButtonGroup>
                        {s.tracking_url && <Button size="micro" url={s.tracking_url} external>Track</Button>}
                        <Button size="micro" loading={busyRow === s.id} onClick={() => doPrint(s.id)}>Print</Button>
                        <Button size="micro" tone="critical" variant="tertiary"
                          loading={busyRow === s.id} onClick={() => doVoid(s.id)}>Void</Button>
                      </ButtonGroup>
                    </InlineStack>
                  </div>
                ))}
              </BlockStack>
              <BlockStack gap="100">
                <Text as="h3" variant="headingSm">{t("note.title")}</Text>
                <InlineStack gap="200" blockAlign="end" wrap={false}>
                  <div style={{ flex: 1 }}>
                    <TextField label={t("note.title")} labelHidden multiline={2} autoComplete="off"
                      placeholder={t("note.placeholder")}
                      value={noteDraft ?? (detail.note || "")}
                      onChange={(v) => setNoteDraft(v)} />
                  </div>
                  <Button size="slim" loading={noteSaving}
                    disabled={noteDraft == null || noteDraft === (detail.note || "")}
                    onClick={() => {
                      if (noteDraft == null) return;
                      setNoteSaving(true);
                      setOrderNote(detail.id, noteDraft)
                        .then(() => { setDetail((d) => (d ? { ...d, note: noteDraft } : d)); setNoteDraft(null); })
                        .catch((e) => alert(String((e as Error).message || e)))
                        .finally(() => setNoteSaving(false));
                    }}>{t("views.save")}</Button>
                </InlineStack>
              </BlockStack>

              <BlockStack gap="150">
                <Text as="h3" variant="headingSm">Timeline</Text>
                {timeline === null ? (
                  <SkeletonBodyText lines={3} />
                ) : timeline.length === 0 ? (
                  <Text as="p" tone="subdued">No activity yet.</Text>
                ) : (
                  <BlockStack gap="0">
                    {timeline.map((ev, i) => (
                      <div key={ev.id ?? i} style={{
                        display: "flex", gap: 10, padding: "8px 0",
                        borderTop: i === 0 ? "none" : "1px solid #f1f1f1",
                      }}>
                        <div style={{
                          marginTop: 6, width: 8, height: 8, borderRadius: 8, flex: "0 0 auto",
                          background: ev.critical ? "#d82c0d" : "#c9cccf",
                        }} />
                        <BlockStack gap="025">
                          <Text as="span" variant="bodySm" tone={ev.critical ? "critical" : undefined}>
                            {stripHtml(ev.message) || "—"}
                          </Text>
                          <Text as="span" variant="bodySm" tone="subdued">
                            {whenShort(ev.created_at)}{ev.app ? ` · ${ev.app}` : ""}
                          </Text>
                        </BlockStack>
                      </div>
                    ))}
                  </BlockStack>
                )}
              </BlockStack>
            </BlockStack>
          ) : (
            <Text as="p" tone="subdued">Nothing to show.</Text>
          )}
        </Modal.Section>
      </Modal>

      {manualFor && (
        <ManualAwbModal orderId={manualFor.id} orderName={manualFor.name} accountOptions={courierOptions}
          onClose={() => setManualFor(null)}
          onDone={() => { setManualFor(null); void load(); }} />
      )}
      {editFor && (
        <OrderEditModal orderId={editFor.id} orderName={editFor.name}
          onClose={() => setEditFor(null)}
          onDone={() => { setEditFor(null); void load(); }} />
      )}
    </Page>
  );
}


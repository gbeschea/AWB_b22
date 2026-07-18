import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Badge,
  Banner,
  BlockStack,
  Button,
  ButtonGroup,
  Card,
  EmptyState,
  IndexTable,
  Modal,
  Page,
  Pagination,
  Select,
  SkeletonBodyText,
  Text,
  TextField,
  useIndexResourceState,
} from "@shopify/polaris";
import {
  ApiError,
  createBulkAwb,
  getCouriers,
  listOrders,
  printLabel,
  voidAwb,
  type BulkAwbResponse,
  type CourierAccount,
  type OrderRow,
  type OrdersResponse,
} from "../lib/api";

const PER_PAGE = 50;
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

function money(v: number | null): string {
  return v == null ? "—" : v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function toast(msg: string, isError = false) {
  (window as any).shopify?.toast?.show(msg, isError ? { isError: true } : undefined);
}

export default function Orders() {
  const [data, setData] = useState<OrdersResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [q, setQ] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");

  const [accounts, setAccounts] = useState<CourierAccount[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [account, setAccount] = useState("");
  const [size, setSize] = useState("A6");
  const [addressId, setAddressId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<BulkAwbResponse | null>(null);
  const [busyRow, setBusyRow] = useState<number | null>(null);

  useEffect(() => {
    const t = setTimeout(() => {
      setDebouncedQ(q);
      setPage(1);
    }, 350);
    return () => clearTimeout(t);
  }, [q]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await listOrders({ page, q: debouncedQ }));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to load orders.");
    } finally {
      setLoading(false);
    }
  }, [page, debouncedQ]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    getCouriers()
      .then((r) => {
        const usable = r.accounts.filter((a) => a.has_credentials && a.is_active);
        setAccounts(usable);
        if (usable[0]) setAccount(usable[0].account_key);
      })
      .catch(() => setAccounts([]));
  }, []);

  const orders = data?.orders ?? [];
  const total = data?.total ?? 0;
  const hasNext = page * PER_PAGE < total;

  const resourceIds = useMemo(() => orders.map((o) => ({ id: String(o.id) })), [orders]);
  const { selectedResources, allResourcesSelected, handleSelectionChange, clearSelection } =
    useIndexResourceState(resourceIds);
  const selectedIds = selectedResources.map((s) => Number(s));

  const courierOptions = accounts.map((a) => ({ label: `${a.name} (${a.account_key})`, value: a.account_key }));
  const needsAddressId = /packeta|zasilkovna/i.test(account); // locker/pickup-point id required

  const doBulkCreate = useCallback(async () => {
    if (!account || selectedIds.length === 0) return;
    setSubmitting(true);
    setResult(null);
    try {
      const opts: Record<string, unknown> = { label_size: size };
      if (addressId.trim()) opts.address_id = addressId.trim();
      const res = await createBulkAwb(selectedIds, account, opts);
      setResult(res);
      toast(`${res.created.length} AWB created${res.errors.length ? `, ${res.errors.length} failed` : ""}`);
      await load();
      if (res.created.length) clearSelection();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Bulk create failed", true);
    } finally {
      setSubmitting(false);
    }
  }, [account, selectedIds, size, addressId, load, clearSelection]);

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

  const promotedBulkActions = [
    { content: `Create AWB (${selectedIds.length})`, onAction: () => { setResult(null); setModalOpen(true); } },
  ];

  return (
    <Page title="Orders" subtitle={total ? `${total} orders` : "Your orders and their AWBs."}>
      <Card padding="0">
        <div style={{ padding: "12px" }}>
          <TextField
            label="Search orders"
            labelHidden
            placeholder="Search by order #, customer, phone or city"
            value={q}
            onChange={setQ}
            autoComplete="off"
            clearButton
            onClearButtonClick={() => setQ("")}
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
            headings={[
              { title: "Order" },
              { title: "Customer" },
              { title: "City" },
              { title: "Total" },
              { title: "Status" },
              { title: "Address" },
              { title: "Courier / AWB" },
              { title: "Actions" },
            ]}
          >
            {orders.map((o: OrderRow, i) => (
              <IndexTable.Row
                id={String(o.id)}
                key={o.id}
                position={i}
                selected={selectedResources.includes(String(o.id))}
              >
                <IndexTable.Cell>
                  <Text as="span" fontWeight="semibold">{o.name ?? `#${o.id}`}</Text>
                </IndexTable.Cell>
                <IndexTable.Cell>{o.customer ?? "—"}</IndexTable.Cell>
                <IndexTable.Cell>{o.city ?? "—"}</IndexTable.Cell>
                <IndexTable.Cell>{money(o.total_price)}</IndexTable.Cell>
                <IndexTable.Cell>
                  <Badge tone={o.processing_status === "done" ? "success" : undefined}>
                    {o.derived_status ?? o.processing_status ?? "—"}
                  </Badge>
                </IndexTable.Cell>
                <IndexTable.Cell>
                  <Badge tone={addressTone(o.address_status)}>{o.address_status ?? "nevalidat"}</Badge>
                </IndexTable.Cell>
                <IndexTable.Cell>
                  {o.awb ? (
                    <Text as="span">{o.courier ? `${o.courier} · ` : ""}{o.awb}</Text>
                  ) : (
                    <Text as="span" tone="subdued">{o.assigned_courier ?? "—"}</Text>
                  )}
                </IndexTable.Cell>
                <IndexTable.Cell>
                  {o.shipment_id ? (
                    <div onClick={(e) => e.stopPropagation()}>
                      <ButtonGroup>
                        <Button size="micro" loading={busyRow === o.shipment_id} onClick={() => doPrint(o.shipment_id!)}>
                          Print
                        </Button>
                        <Button size="micro" tone="critical" variant="tertiary"
                          loading={busyRow === o.shipment_id} onClick={() => doVoid(o.shipment_id!)}>
                          Void
                        </Button>
                      </ButtonGroup>
                    </div>
                  ) : (
                    <Text as="span" tone="subdued">—</Text>
                  )}
                </IndexTable.Cell>
              </IndexTable.Row>
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
        onClose={() => setModalOpen(false)}
        title={`Create AWB for ${selectedIds.length} order${selectedIds.length === 1 ? "" : "s"}`}
        primaryAction={{ content: "Create AWB", onAction: doBulkCreate, loading: submitting, disabled: !account }}
        secondaryActions={[{ content: "Cancel", onAction: () => setModalOpen(false) }]}
      >
        <Modal.Section>
          <BlockStack gap="400">
            {accounts.length === 0 ? (
              <Banner tone="warning" title="No courier configured">
                <p>Add a courier account with credentials in Settings first.</p>
              </Banner>
            ) : (
              <>
                <Select label="Courier" options={courierOptions} value={account} onChange={setAccount} />
                <Select label="Label size" options={SIZE_OPTIONS} value={size} onChange={setSize} />
                <TextField
                  label={needsAddressId ? "Pickup point / locker ID (required)" : "Pickup point / locker ID (optional)"}
                  value={addressId}
                  onChange={setAddressId}
                  autoComplete="off"
                  helpText="Leave empty for home delivery. For locker/pickup-point delivery, enter the point ID."
                />
                <Text as="p" tone="subdued" variant="bodySm">
                  A courier pickup is requested automatically where the courier needs it (DPD, FAN).
                </Text>
              </>
            )}
          </BlockStack>
        </Modal.Section>
      </Modal>
    </Page>
  );
}

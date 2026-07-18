import { useCallback, useEffect, useState } from "react";
import {
  Badge,
  Banner,
  Card,
  EmptyState,
  IndexTable,
  Page,
  Pagination,
  SkeletonBodyText,
  Text,
  TextField,
} from "@shopify/polaris";
import { ApiError, listOrders, type OrderRow, type OrdersResponse } from "../lib/api";

const PER_PAGE = 50;

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
  return v == null
    ? "—"
    : v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export default function Orders() {
  const [data, setData] = useState<OrdersResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [q, setQ] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");

  // Debounce the search box so typing doesn't fire a request per keystroke.
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

  const orders = data?.orders ?? [];
  const total = data?.total ?? 0;
  const hasNext = page * PER_PAGE < total;

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
            selectable={false}
            headings={[
              { title: "Order" },
              { title: "Customer" },
              { title: "City" },
              { title: "Total" },
              { title: "Status" },
              { title: "Address" },
              { title: "Courier / AWB" },
            ]}
          >
            {orders.map((o: OrderRow, i) => (
              <IndexTable.Row id={String(o.id)} key={o.id} position={i}>
                <IndexTable.Cell>
                  <Text as="span" fontWeight="semibold">
                    {o.name ?? `#${o.id}`}
                  </Text>
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
                  <Badge tone={addressTone(o.address_status)}>
                    {o.address_status ?? "nevalidat"}
                  </Badge>
                </IndexTable.Cell>
                <IndexTable.Cell>
                  {o.awb ? (
                    <Text as="span">
                      {o.courier ? `${o.courier} · ` : ""}
                      {o.awb}
                    </Text>
                  ) : (
                    <Text as="span" tone="subdued">
                      {o.assigned_courier ?? "—"}
                    </Text>
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
    </Page>
  );
}

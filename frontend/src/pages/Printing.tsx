import { useEffect, useState } from "react";
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
} from "@shopify/polaris";
import { ApiError, getPrintQueue, type PrintItem, type PrintQueueResponse } from "../lib/api";

const PER_PAGE = 50;

export default function Printing() {
  const [data, setData] = useState<PrintQueueResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);

  useEffect(() => {
    (async () => {
      setLoading(true);
      setError(null);
      try {
        setData(await getPrintQueue(page));
      } catch (e) {
        setError(e instanceof ApiError ? e.message : "Failed to load the print queue.");
      } finally {
        setLoading(false);
      }
    })();
  }, [page]);

  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const hasNext = page * PER_PAGE < total;

  return (
    <Page
      title="Printing"
      subtitle={total ? `${total} orders ready to print` : "Labels ready to print."}
    >
      {error && (
        <div style={{ marginBottom: 16 }}>
          <Banner tone="critical" title="Couldn't load the print queue" onDismiss={() => setError(null)}>
            <p>{error}</p>
          </Banner>
        </div>
      )}

      <Card padding="0">
        {loading ? (
          <div style={{ padding: 12 }}>
            <SkeletonBodyText lines={8} />
          </div>
        ) : items.length === 0 ? (
          <EmptyState
            heading="Nothing to print"
            image="https://cdn.shopify.com/s/files/1/0757/9955/files/empty-state.svg"
          >
            <p>Orders with a generated AWB that hasn't been printed will queue up here.</p>
          </EmptyState>
        ) : (
          <IndexTable
            resourceName={{ singular: "label", plural: "labels" }}
            itemCount={items.length}
            selectable={false}
            headings={[
              { title: "Order" },
              { title: "Customer" },
              { title: "City" },
              { title: "AWB(s)" },
            ]}
          >
            {items.map((it: PrintItem, i) => (
              <IndexTable.Row id={String(it.id)} key={it.id} position={i}>
                <IndexTable.Cell>
                  <Text as="span" fontWeight="semibold">{it.name ?? `#${it.id}`}</Text>
                </IndexTable.Cell>
                <IndexTable.Cell>{it.customer ?? "—"}</IndexTable.Cell>
                <IndexTable.Cell>{it.city ?? "—"}</IndexTable.Cell>
                <IndexTable.Cell>
                  {it.shipments.map((s) => (
                    <div key={s.id}>
                      <Badge>{s.courier ?? "AWB"}</Badge> <Text as="span">{s.awb}</Text>
                    </div>
                  ))}
                </IndexTable.Cell>
              </IndexTable.Row>
            ))}
          </IndexTable>
        )}

        {(hasNext || page > 1) && (
          <div style={{ display: "flex", justifyContent: "center", padding: 12 }}>
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

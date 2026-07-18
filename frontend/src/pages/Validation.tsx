import { useEffect, useState } from "react";
import {
  Badge,
  Banner,
  BlockStack,
  Card,
  EmptyState,
  InlineStack,
  Page,
  Pagination,
  SkeletonBodyText,
  Text,
} from "@shopify/polaris";
import { ApiError, getAddressIssues, type AddressIssue, type AddressIssuesResponse } from "../lib/api";

const PER_PAGE = 50;

function tone(status: string | null): "warning" | "critical" | "attention" | undefined {
  switch (status) {
    case "partial_match":
      return "attention";
    case "invalid":
    case "not_found":
      return "critical";
    default:
      return "warning";
  }
}

function summarize(value: unknown): string | null {
  if (value == null) return null;
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.filter(Boolean).map(String).join(", ") || null;
  if (typeof value === "object") {
    const parts = Object.entries(value as Record<string, unknown>)
      .filter(([, v]) => v != null && v !== "")
      .map(([k, v]) => `${k}: ${String(v)}`);
    return parts.length ? parts.join(" · ") : null;
  }
  return String(value);
}

function addressLine(s: AddressIssue["shipping"]): string {
  return [s.address1, s.address2, s.zip, s.city, s.province, s.country]
    .filter(Boolean)
    .join(", ");
}

export default function Validation() {
  const [data, setData] = useState<AddressIssuesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);

  useEffect(() => {
    (async () => {
      setLoading(true);
      setError(null);
      try {
        setData(await getAddressIssues(page));
      } catch (e) {
        setError(e instanceof ApiError ? e.message : "Failed to load address issues.");
      } finally {
        setLoading(false);
      }
    })();
  }, [page]);

  const issues = data?.issues ?? [];
  const total = data?.total ?? 0;
  const hasNext = page * PER_PAGE < total;

  return (
    <Page
      title="Address validation"
      subtitle={total ? `${total} orders need attention` : "Shipping addresses that need attention."}
    >
      {error && (
        <div style={{ marginBottom: 16 }}>
          <Banner tone="critical" title="Couldn't load address issues" onDismiss={() => setError(null)}>
            <p>{error}</p>
          </Banner>
        </div>
      )}

      {loading ? (
        <Card>
          <SkeletonBodyText lines={10} />
        </Card>
      ) : issues.length === 0 ? (
        <Card>
          <EmptyState
            heading="No address issues"
            image="https://cdn.shopify.com/s/files/1/0757/9955/files/empty-state.svg"
          >
            <p>Every order's shipping address looks deliverable. New issues will appear here.</p>
          </EmptyState>
        </Card>
      ) : (
        <BlockStack gap="300">
          {issues.map((it) => {
            const errs = summarize(it.errors);
            const sugg = summarize(it.suggestions);
            return (
              <Card key={it.id}>
                <BlockStack gap="200">
                  <InlineStack align="space-between" blockAlign="center">
                    <InlineStack gap="200" blockAlign="center">
                      <Text as="span" fontWeight="semibold">{it.name ?? `#${it.id}`}</Text>
                      <Text as="span" tone="subdued">{it.customer ?? ""}</Text>
                    </InlineStack>
                    <InlineStack gap="200" blockAlign="center">
                      {it.address_score != null && (
                        <Text as="span" tone="subdued">score {it.address_score}</Text>
                      )}
                      <Badge tone={tone(it.address_status)}>{it.address_status ?? "nevalidat"}</Badge>
                    </InlineStack>
                  </InlineStack>

                  <Text as="p">{addressLine(it.shipping) || "—"}</Text>

                  {errs && (
                    <Text as="p" tone="critical">Problem: {errs}</Text>
                  )}
                  {sugg && (
                    <Text as="p" tone="success">Suggested: {sugg}</Text>
                  )}
                </BlockStack>
              </Card>
            );
          })}

          {(hasNext || page > 1) && (
            <InlineStack align="center">
              <Pagination
                hasPrevious={page > 1}
                onPrevious={() => setPage((p) => Math.max(1, p - 1))}
                hasNext={hasNext}
                onNext={() => setPage((p) => p + 1)}
                label={`Page ${page}`}
              />
            </InlineStack>
          )}
        </BlockStack>
      )}
    </Page>
  );
}

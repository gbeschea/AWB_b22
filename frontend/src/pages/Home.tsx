import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Badge,
  Banner,
  BlockStack,
  Box,
  Card,
  Icon,
  InlineGrid,
  InlineStack,
  Page,
  SkeletonBodyText,
  SkeletonDisplayText,
  SkeletonPage,
  Text,
} from "@shopify/polaris";
import { CheckCircleIcon, ClockIcon } from "@shopify/polaris-icons";

import { authFetch, ApiError, getOverview } from "../lib/api";
import type { MeResponse, OverviewResponse } from "../lib/api";

function InfoRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <InlineStack align="space-between" blockAlign="center" gap="400" wrap={false}>
      <Text as="span" tone="subdued" variant="bodyMd">
        {label}
      </Text>
      <div style={{ textAlign: "right" }}>{children}</div>
    </InlineStack>
  );
}

function Stat({ label, value, tone, onClick }: {
  label: string;
  value: number;
  tone?: "critical" | "warning";
  onClick?: () => void;
}) {
  return (
    <div style={{ cursor: onClick ? "pointer" : "default" }} onClick={onClick}>
      <Box background="bg-surface-secondary" borderRadius="200" padding="400">
        <BlockStack gap="100">
          <Text as="p" variant="heading2xl" tone={tone === "critical" ? "critical" : undefined}>
            {value}
          </Text>
          <Text as="p" tone="subdued" variant="bodySm">{label}</Text>
        </BlockStack>
      </Box>
    </div>
  );
}

export default function Home() {
  const navigate = useNavigate();
  const [me, setMe] = useState<MeResponse | null>(null);
  const [overview, setOverview] = useState<OverviewResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [meRes, ovRes] = await Promise.all([
          authFetch<MeResponse>("/api/me"),
          getOverview().catch(() => null),
        ]);
        if (cancelled) return;
        setMe(meRes);
        setOverview(ovRes);
      } catch (err) {
        if (cancelled) return;
        setError(
          err instanceof ApiError
            ? `${err.message} (${err.status})`
            : err instanceof Error
              ? err.message
              : "Could not load your shop details.",
        );
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (!me && !error) {
    return (
      <SkeletonPage title="Order Hub" primaryAction>
        <BlockStack gap="400">
          <Card>
            <BlockStack gap="300">
              <SkeletonDisplayText size="small" />
              <SkeletonBodyText lines={3} />
            </BlockStack>
          </Card>
          <Card>
            <SkeletonBodyText lines={4} />
          </Card>
        </BlockStack>
      </SkeletonPage>
    );
  }

  const steps = [
    { done: true, label: "Install Order Hub", desc: "Your store is connected." },
    {
      done: !!overview?.has_courier_account,
      label: "Connect a courier",
      desc: "Add DPD, Sameday, Econt or Packeta credentials in Settings.",
    },
    {
      done: (overview?.orders_total ?? 0) > 0,
      label: "Sync your orders",
      desc: "Orders flow in from Shopify and appear under Orders.",
    },
  ];

  return (
    <Page title="Order Hub" subtitle="Courier & AWB logistics for your Shopify store.">
      <BlockStack gap="400">
        {error && (
          <Banner title="Couldn't load your shop details" tone="critical">
            <p>{error}</p>
          </Banner>
        )}

        {overview && (
          <Card>
            <BlockStack gap="300">
              <Text as="h2" variant="headingMd">At a glance</Text>
              <InlineGrid columns={{ xs: 1, sm: 3 }} gap="300">
                <Stat label="Orders" value={overview.orders_total} onClick={() => navigate("/app/orders")} />
                <Stat
                  label="Address issues"
                  value={overview.address_issues}
                  tone={overview.address_issues > 0 ? "critical" : undefined}
                  onClick={() => navigate("/app/validation")}
                />
                <Stat
                  label="Ready to print"
                  value={overview.print_queue}
                  onClick={() => navigate("/app/printing")}
                />
              </InlineGrid>
            </BlockStack>
          </Card>
        )}

        {me && (
          <Card>
            <BlockStack gap="400">
              <Text as="h2" variant="headingMd">Connected store</Text>
              <BlockStack gap="300">
                <InfoRow label="Shop">
                  <Text as="span" variant="bodyMd" fontWeight="semibold">{me.shop}</Text>
                </InfoRow>
                <InfoRow label="Status">
                  <Badge tone={me.is_active ? "success" : "critical"}>
                    {me.is_active ? "Active" : "Inactive"}
                  </Badge>
                </InfoRow>
                <InfoRow label="Plan">
                  <Badge tone="info">{me.plan}</Badge>
                </InfoRow>
              </BlockStack>
            </BlockStack>
          </Card>
        )}

        <Card>
          <BlockStack gap="300">
            <Text as="h2" variant="headingMd">Setup guide</Text>
            <Text as="p" tone="subdued" variant="bodyMd">
              A few steps to get shipping labels flowing.
            </Text>
            <BlockStack gap="0">
              {steps.map((step) => (
                <Box key={step.label} borderColor="border" borderBlockEndWidth="025" padding="300">
                  <InlineStack gap="300" blockAlign="start" wrap={false}>
                    <span style={{ display: "inline-flex", opacity: step.done ? 1 : 0.4 }}>
                      <Icon source={step.done ? CheckCircleIcon : ClockIcon} tone={step.done ? "success" : "subdued"} />
                    </span>
                    <BlockStack gap="050">
                      <Text as="span" variant="bodyMd" fontWeight="semibold" tone={step.done ? "subdued" : undefined}>
                        {step.label}
                      </Text>
                      <Text as="span" tone="subdued" variant="bodySm">{step.desc}</Text>
                    </BlockStack>
                  </InlineStack>
                </Box>
              ))}
            </BlockStack>
          </BlockStack>
        </Card>
      </BlockStack>
    </Page>
  );
}

import { useCallback, useEffect, useRef, useState } from "react";
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

import { authFetch, ApiError, getOverview, syncNow } from "../lib/api";
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

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function formatLastSync(iso: string | null | undefined): string {
  if (!iso) return "Never";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "Never";
  const secs = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
  if (secs < 60) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return d.toLocaleDateString();
}

export default function Home() {
  const navigate = useNavigate();
  const [me, setMe] = useState<MeResponse | null>(null);
  const [overview, setOverview] = useState<OverviewResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const pollingRef = useRef(false);

  // Poll /overview every few seconds while a backfill is in flight, until it settles.
  const pollUntilIdle = useCallback(async () => {
    if (pollingRef.current) return;
    pollingRef.current = true;
    setSyncing(true);
    try {
      for (let i = 0; i < 40; i++) {
        await sleep(3000);
        let ov: OverviewResponse | null = null;
        try {
          ov = await getOverview();
        } catch {
          continue;
        }
        setOverview(ov);
        if (!ov.syncing) break;
      }
    } finally {
      pollingRef.current = false;
      setSyncing(false);
    }
  }, []);

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
        // A backfill kicked off at install time may still be running — pick it up.
        if (ovRes?.syncing) void pollUntilIdle();
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
  }, [pollUntilIdle]);

  const handleSync = useCallback(async () => {
    setSyncing(true);
    try {
      const res = await syncNow();
      (window as any).shopify?.toast?.show(
        res.status === "in_progress" ? "A sync is already running…" : "Syncing orders from Shopify…",
      );
      await pollUntilIdle();
    } catch (err) {
      (window as any).shopify?.toast?.show(
        err instanceof Error ? err.message : "Sync failed to start",
        { isError: true },
      );
      setSyncing(false);
    }
  }, [pollUntilIdle]);

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
      desc: "Use “Sync orders” above to pull recent orders — new ones then flow in automatically.",
    },
  ];

  return (
    <Page
      title="Order Hub"
      subtitle="Courier & AWB logistics for your Shopify store."
      primaryAction={{
        content: "Sync orders",
        onAction: handleSync,
        loading: syncing,
      }}
    >
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
                <InfoRow label="Orders synced">
                  {syncing ? (
                    <Badge tone="attention" progress="partiallyComplete">Syncing…</Badge>
                  ) : (
                    <Text as="span" variant="bodyMd">{formatLastSync(overview?.last_sync_at)}</Text>
                  )}
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

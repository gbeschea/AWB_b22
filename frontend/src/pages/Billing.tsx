import { useEffect, useState } from "react";
import {
  Badge,
  Banner,
  BlockStack,
  Box,
  Button,
  Card,
  InlineGrid,
  InlineStack,
  List,
  Page,
  SkeletonBodyText,
  Text,
} from "@shopify/polaris";
import {
  ApiError,
  cancelBilling,
  getBilling,
  subscribe,
  type BillingPlan,
  type BillingStatus,
} from "../lib/api";

export default function Billing() {
  const [data, setData] = useState<BillingStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = async () => {
    try {
      setData(await getBilling());
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to load billing.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const onSubscribe = async (plan: string) => {
    setBusy(plan);
    setError(null);
    try {
      const { confirmationUrl } = await subscribe(plan);
      // Billing confirmation must open in the top frame (outside the app iframe).
      open(confirmationUrl, "_top");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't start the subscription.");
      setBusy(null);
    }
  };

  const onCancel = async () => {
    setBusy("cancel");
    setError(null);
    try {
      await cancelBilling();
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't cancel.");
    } finally {
      setBusy(null);
    }
  };

  const current = data?.current_plan ?? "free";
  const plans = data?.plans ?? [];

  return (
    <Page title="Plans & billing" subtitle="Choose the plan that fits your shipping volume.">
      <BlockStack gap="400">
        {data?.test && (
          <Banner tone="info">
            <p>Test mode — subscriptions are Shopify test charges and won't bill real money.</p>
          </Banner>
        )}
        {error && (
          <Banner tone="critical" title="Billing error" onDismiss={() => setError(null)}>
            <p>{error}</p>
          </Banner>
        )}

        {loading ? (
          <Card>
            <SkeletonBodyText lines={6} />
          </Card>
        ) : (
          <InlineGrid columns={{ xs: 1, md: 2 }} gap="400">
            {plans.map((p: BillingPlan) => {
              const isCurrent = p.key === current;
              return (
                <Card key={p.key}>
                  <BlockStack gap="300">
                    <InlineStack align="space-between" blockAlign="center">
                      <Text as="h2" variant="headingMd">{p.name}</Text>
                      {isCurrent && <Badge tone="success">Current plan</Badge>}
                    </InlineStack>
                    <Text as="p" variant="headingLg">
                      {p.price > 0 ? `$${p.price.toFixed(2)}/mo` : "Free"}
                      {p.trial_days > 0 && (
                        <Text as="span" tone="subdued" variant="bodySm">{`  ·  ${p.trial_days}-day trial`}</Text>
                      )}
                    </Text>
                    <List>
                      {p.features.map((f) => (
                        <List.Item key={f}>{f}</List.Item>
                      ))}
                    </List>
                    <Box>
                      {isCurrent ? (
                        p.key !== "free" ? (
                          <Button loading={busy === "cancel"} onClick={onCancel}>
                            Cancel plan
                          </Button>
                        ) : (
                          <Button disabled>Current plan</Button>
                        )
                      ) : p.price > 0 ? (
                        <Button variant="primary" loading={busy === p.key} onClick={() => onSubscribe(p.key)}>
                          {`Choose ${p.name}`}
                        </Button>
                      ) : (
                        <Button loading={busy === "cancel"} onClick={onCancel}>
                          Downgrade to Free
                        </Button>
                      )}
                    </Box>
                  </BlockStack>
                </Card>
              );
            })}
          </InlineGrid>
        )}
      </BlockStack>
    </Page>
  );
}

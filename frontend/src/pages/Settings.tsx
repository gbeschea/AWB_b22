import { useEffect, useState } from "react";
import {
  Badge,
  BlockStack,
  Banner,
  Card,
  InlineStack,
  Layout,
  Page,
  ResourceItem,
  ResourceList,
  SkeletonBodyText,
  Text,
} from "@shopify/polaris";
import { ApiError, getCouriers, type CouriersResponse } from "../lib/api";

export default function Settings() {
  const [data, setData] = useState<CouriersResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        setData(await getCouriers());
      } catch (e) {
        setError(e instanceof ApiError ? e.message : "Failed to load settings.");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const accounts = data?.accounts ?? [];
  const profiles = data?.profiles ?? [];
  const mappings = data?.mappings ?? [];

  return (
    <Page title="Settings" subtitle="Couriers, shipment profiles and mappings.">
      <Layout>
        {error && (
          <Layout.Section>
            <Banner tone="critical" title="Couldn't load settings" onDismiss={() => setError(null)}>
              <p>{error}</p>
            </Banner>
          </Layout.Section>
        )}

        <Layout.Section>
          <Card>
            <BlockStack gap="300">
              <Text as="h2" variant="headingMd">Courier accounts</Text>
              {loading ? (
                <SkeletonBodyText lines={4} />
              ) : (
                <ResourceList
                  resourceName={{ singular: "account", plural: "accounts" }}
                  items={accounts}
                  emptyState={<Text as="p" tone="subdued">No courier accounts yet.</Text>}
                  renderItem={(a) => (
                    <ResourceItem id={String(a.id)} onClick={() => {}}>
                      <InlineStack align="space-between" blockAlign="center">
                        <BlockStack gap="050">
                          <Text as="span" fontWeight="semibold">{a.name}</Text>
                          <Text as="span" tone="subdued">{a.courier_type} · {a.account_key}</Text>
                        </BlockStack>
                        <InlineStack gap="200">
                          <Badge tone={a.has_credentials ? "success" : "warning"}>
                            {a.has_credentials ? "Credentials set" : "No credentials"}
                          </Badge>
                          <Badge tone={a.is_active ? "success" : undefined}>
                            {a.is_active ? "Active" : "Inactive"}
                          </Badge>
                        </InlineStack>
                      </InlineStack>
                    </ResourceItem>
                  )}
                />
              )}
            </BlockStack>
          </Card>
        </Layout.Section>

        <Layout.Section>
          <Card>
            <BlockStack gap="300">
              <Text as="h2" variant="headingMd">Shipment profiles</Text>
              {loading ? (
                <SkeletonBodyText lines={3} />
              ) : profiles.length === 0 ? (
                <Text as="p" tone="subdued">No shipment profiles yet.</Text>
              ) : (
                <ResourceList
                  resourceName={{ singular: "profile", plural: "profiles" }}
                  items={profiles}
                  renderItem={(p) => (
                    <ResourceItem id={String(p.id)} onClick={() => {}}>
                      <InlineStack align="space-between" blockAlign="center">
                        <Text as="span" fontWeight="semibold">{p.name}</Text>
                        <Text as="span" tone="subdued">
                          {p.account_key} · {p.default_parcels ?? 1} parcel(s) · {p.default_weight_kg ?? 1} kg
                        </Text>
                      </InlineStack>
                    </ResourceItem>
                  )}
                />
              )}
            </BlockStack>
          </Card>
        </Layout.Section>

        <Layout.Section>
          <Card>
            <BlockStack gap="300">
              <Text as="h2" variant="headingMd">Courier mappings</Text>
              <Text as="p" tone="subdued">
                Maps the courier name that arrives from Shopify to one of your accounts.
              </Text>
              {loading ? (
                <SkeletonBodyText lines={2} />
              ) : mappings.length === 0 ? (
                <Text as="p" tone="subdued">No mappings yet.</Text>
              ) : (
                <ResourceList
                  resourceName={{ singular: "mapping", plural: "mappings" }}
                  items={mappings}
                  renderItem={(m) => (
                    <ResourceItem id={String(m.id)} onClick={() => {}}>
                      <InlineStack align="space-between" blockAlign="center">
                        <Text as="span">{m.shopify_name}</Text>
                        <Text as="span" tone="subdued">→ {m.account_key}</Text>
                      </InlineStack>
                    </ResourceItem>
                  )}
                />
              )}
            </BlockStack>
          </Card>
        </Layout.Section>
      </Layout>
    </Page>
  );
}

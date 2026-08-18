import { useCallback, useEffect, useState } from "react";
import {
  Badge, Banner, BlockStack, Button, Card, InlineGrid, InlineStack, Select, Text, TextField,
} from "@shopify/polaris";
import {
  getInvoiceSettings, saveInvoiceSettings, getSmartbillSeries, smartbillStatus,
  type InvoiceSettings, ApiError,
} from "../lib/api";

function toast(msg: string, isError = false) {
  (window as unknown as { shopify?: { toast: { show: (m: string, o?: { isError: boolean }) => void } } })
    .shopify?.toast.show(msg, isError ? { isError: true } : undefined);
}
const YESNO = [{ label: "No", value: "no" }, { label: "Yes", value: "yes" }];
const b2s = (b: boolean) => (b ? "yes" : "no");
const VAT_CATEGORIES = [
  { label: "Standard VAT (e.g. 21%)", value: "standard" },
  { label: "Zero-rated 0% (export / 0% domestic)", value: "zero" },
  { label: "Exempt (scutit)", value: "exempt" },
  { label: "Reverse charge (taxare inversă)", value: "reverse_charge" },
  { label: "Intra-community supply (OSS)", value: "intracommunity" },
];

/** SmartBill invoicing settings (per store) — the xConnector-style invoice config. Creds live on
 * the server (env); this configures series, VAT, currency, due days and line-item behaviour. */
export function InvoiceSettingsCard() {
  const [cfg, setCfg] = useState<InvoiceSettings | null>(null);
  const [series, setSeries] = useState<{ name: string; next: string | number }[]>([]);
  const [connected, setConnected] = useState<boolean | null>(null);
  const [seriesErr, setSeriesErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    smartbillStatus().then((r) => setConnected(r.configured)).catch(() => setConnected(false));
    getInvoiceSettings().then(setCfg).catch(() => setCfg(null));
    getSmartbillSeries().then((r) => { setSeries(r.series || []); if (r.error) setSeriesErr(r.error); }).catch(() => {});
  }, []);

  const set = <K extends keyof InvoiceSettings>(k: K, v: InvoiceSettings[K]) =>
    setCfg((c) => (c ? { ...c, [k]: v } : c));

  const save = useCallback(async () => {
    if (!cfg) return;
    setSaving(true);
    try {
      await saveInvoiceSettings(cfg);
      toast("Invoice settings saved");
    } catch (e) {
      toast(e instanceof ApiError ? e.message : "Save failed", true);
    } finally { setSaving(false); }
  }, [cfg]);

  if (connected === false) {
    return (
      <Card>
        <BlockStack gap="200">
          <Text as="h2" variant="headingMd">Invoicing (SmartBill)</Text>
          <Banner tone="warning" title="SmartBill not configured">
            <p>The SmartBill credentials aren't set on the server yet. Once connected, configure the
              invoice series, VAT and preferences here.</p>
          </Banner>
        </BlockStack>
      </Card>
    );
  }
  if (!cfg) return <Card><Text as="p" tone="subdued">Loading invoice settings…</Text></Card>;

  const seriesOptions = [
    { label: series.length ? "— choose a series —" : "(no series found)", value: "" },
    ...series.map((s) => ({ label: `${s.name}${s.next ? ` (next ${s.next})` : ""}`, value: s.name })),
    ...(cfg.series && !series.some((s) => s.name === cfg.series) ? [{ label: cfg.series, value: cfg.series }] : []),
  ];

  return (
    <Card>
      <BlockStack gap="400">
        <InlineStack align="space-between" blockAlign="center">
          <InlineStack gap="200" blockAlign="center">
            <Text as="h2" variant="headingMd">Invoicing (SmartBill)</Text>
            {connected && <Badge tone="success">Connected</Badge>}
          </InlineStack>
          <Button variant="primary" loading={saving} onClick={() => void save()}>Save</Button>
        </InlineStack>

        <BlockStack gap="200">
          <Text as="h3" variant="headingSm">Invoice preferences</Text>
          <Select
            label="Issue invoices through"
            options={[
              { label: "xConnector — the store's SmartBill (recommended)", value: "xconnector" },
              { label: "SmartBill directly (server account)", value: "smartbill" },
            ]}
            value={cfg.invoice_via ?? "xconnector"}
            onChange={(v) => set("invoice_via", v)}
            helpText="Who creates/cancels/downloads invoices. Independent of the AWB courier (set in Automation)."
          />
          <InlineGrid columns={{ xs: 1, sm: 2 }} gap="300">
            <Select label="Invoice series" options={seriesOptions} value={cfg.series}
              onChange={(v) => set("series", v)}
              helpText={seriesErr ? `Couldn't list series: ${seriesErr}` : "From your SmartBill account."} />
            <TextField label="Currency" value={cfg.currency} onChange={(v) => set("currency", v)} autoComplete="off" />
            <TextField label="Due days" type="number" value={String(cfg.due_days)}
              onChange={(v) => set("due_days", Number(v) || 0)} autoComplete="off" />
            <TextField label="Measure unit" value={cfg.measure_unit} onChange={(v) => set("measure_unit", v)} autoComplete="off" />
            <Select label="Product code type" value={cfg.product_code_type} onChange={(v) => set("product_code_type", v)}
              options={[{ label: "SKU", value: "sku" }, { label: "Barcode", value: "barcode" }]}
              helpText="Which code goes on each invoice line." />
            <Select label="Save product to SmartBill DB" value={b2s(cfg.save_product)}
              onChange={(v) => set("save_product", v === "yes")} options={YESNO} />
            <Select label="Save customer to SmartBill DB" value={b2s(cfg.save_customer)}
              onChange={(v) => set("save_customer", v === "yes")} options={YESNO} />
            <Select label="Auto-send invoice by email" value={b2s(cfg.auto_send_email)}
              onChange={(v) => set("auto_send_email", v === "yes")} options={YESNO} />
          </InlineGrid>
        </BlockStack>

        <BlockStack gap="200">
          <Text as="h3" variant="headingSm">VAT</Text>
          <InlineGrid columns={{ xs: 1, sm: 2 }} gap="300">
            <TextField label="VAT rate (%)" type="number" value={String(cfg.vat_rate)}
              onChange={(v) => set("vat_rate", Number(v) || 0)} autoComplete="off"
              helpText="RO standard = 21." />
            <TextField label="VAT name (SmartBill)" value={cfg.vat_name} onChange={(v) => set("vat_name", v)}
              autoComplete="off" helpText='Must match a tax name in your account (e.g. "Normala").' />
            <TextField label="Extra CIF (optional)" value={cfg.extra_cif} onChange={(v) => set("extra_cif", v)}
              autoComplete="off" helpText="Supplementary VAT code sent as extraCif." />
            <TextField label="Invoice mentions (optional)" value={cfg.mentions} onChange={(v) => set("mentions", v)}
              autoComplete="off" helpText="Free text shown on the invoice." />
          </InlineGrid>
          <Select label="VAT category" value={cfg.vat_category} onChange={(v) => set("vat_category", v)}
            options={VAT_CATEGORIES}
            helpText={cfg.vat_category === "standard"
              ? "Charges the VAT rate above on every line."
              : 'Forces 0% on product lines. The tax name above ("' + cfg.vat_name +
                '") must exist as a 0% tax in your SmartBill account. Reverse-charge / intra-community also mark the client as a VAT payer.'} />
          {cfg.vat_category === "intracommunity" && (
            <Banner tone="info">
              <p>Intra-community supplies need the buyer's valid EU VAT code on the invoice. Add it in
                SmartBill after creating the draft, or set it per-order.</p>
            </Banner>
          )}
        </BlockStack>

        <BlockStack gap="200">
          <Text as="h3" variant="headingSm">Advanced &amp; lines</Text>
          <InlineGrid columns={{ xs: 1, sm: 2 }} gap="300">
            <Select label="Prices already include VAT" value={b2s(cfg.prices_include_vat)}
              onChange={(v) => set("prices_include_vat", v === "yes")} options={YESNO}
              helpText="Shopify COD prices are usually VAT-included (Yes)." />
            <Select label="Pay VAT on collection (TVA la încasare)" value={b2s(cfg.vat_on_payment)}
              onChange={(v) => set("vat_on_payment", v === "yes")} options={YESNO}
              helpText='Adds the legally-required "TVA la încasare" mention. Enable this only if your SmartBill account is registered for that regime.' />

            <Select label="Client is a company (B2B)" value={b2s(cfg.client_tax_payer)}
              onChange={(v) => set("client_tax_payer", v === "yes")} options={YESNO}
              helpText="Marks the client as a VAT payer on the invoice." />
            <TextField label="Default product code" value={cfg.default_product_code}
              onChange={(v) => set("default_product_code", v)} autoComplete="off"
              helpText="Used on a line that has no SKU/barcode." />
          </InlineGrid>
        </BlockStack>

        <BlockStack gap="200">
          <Text as="h3" variant="headingSm">Shipping line</Text>
          <InlineGrid columns={{ xs: 1, sm: 2 }} gap="300">
            <Select label="Bill shipping as a separate line" value={b2s(cfg.invoice_shipping)}
              onChange={(v) => set("invoice_shipping", v === "yes")} options={YESNO}
              helpText="Adds the order's shipping cost as its own invoice line." />
            <TextField label="Shipping line name" value={cfg.shipping_name}
              onChange={(v) => set("shipping_name", v)} autoComplete="off"
              disabled={!cfg.invoice_shipping} />
            <TextField label="Shipping / service code" value={cfg.shipping_code}
              onChange={(v) => set("shipping_code", v)} autoComplete="off"
              disabled={!cfg.invoice_shipping} />
            <TextField label="Shipping VAT rate (%)" type="number"
              value={cfg.shipping_vat_rate === null || cfg.shipping_vat_rate === undefined ? "" : String(cfg.shipping_vat_rate)}
              onChange={(v) => set("shipping_vat_rate", v === "" ? null : (Number(v) || 0))}
              autoComplete="off" disabled={!cfg.invoice_shipping}
              helpText="Leave empty to use the same VAT as products." />
          </InlineGrid>
        </BlockStack>
      </BlockStack>
    </Card>
  );
}

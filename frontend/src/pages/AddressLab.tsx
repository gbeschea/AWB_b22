import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Badge, Banner, BlockStack, Button, Card, Checkbox, Divider, InlineStack,
  Page, Select, SkeletonBodyText, Text, TextField,
} from "@shopify/polaris";
import { useNavigate } from "react-router-dom";
import {
  ApiError, checkAddress, getValidationPolicies, getValidationRules, putValidationPolicies,
  type AddressCheckResult, type PolicyMeta, type ValidationRule,
} from "../lib/api";
import { t } from "../lib/i18n";

/* The Address Lab is the window into the consolidated validator: try any address against the
   live engine (RO rich nomenclator + homonym guard; CZ/PL/BG/HU/SK intl nomenclators), inspect
   the always-on correctness rules, and tune the eligible business policies. */

const COUNTRIES = [
  { label: "România", value: "RO" }, { label: t("Czechia (CZ)"), value: "CZ" },
  { label: t("Poland (PL)"), value: "PL" }, { label: "Bulgaria (BG)", value: "BG" },
  { label: t("Hungary (HU)"), value: "HU" }, { label: t("Slovakia (SK)"), value: "SK" },
  { label: t("Other country (→ geocoder)"), value: "XX" },
];

function verdictTone(status: string): "success" | "info" | "warning" | "critical" {
  switch (status) {
    case "valid": return "success";
    case "corrected": return "info";
    case "needs_geocoder": return "warning";
    default: return "critical"; // cs
  }
}
const VERDICT_LABEL: Record<string, string> = {
  valid: t("VALID — ships as-is"),
  corrected: t("CORRECTED — write-back proposed"),
  needs_geocoder: t("GEOCODER — the nomenclator cannot decide, goes to HERE"),
  cs: t("CS — needs a human"),
};

function toast(msg: string) {
  try { (window as unknown as { shopify?: { toast: { show: (m: string) => void } } }).shopify?.toast.show(msg); }
  catch { /* noop */ }
}

export default function AddressLab() {
  const navigate = useNavigate();

  /* ---- Verifică o adresă ---- */
  const [form, setForm] = useState({ country: "RO", province: "", city: "", zip: "", address1: "", address2: "" });
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<AddressCheckResult | null>(null);
  const [checkErr, setCheckErr] = useState<string | null>(null);
  const set = (k: keyof typeof form) => (v: string) => setForm((f) => ({ ...f, [k]: v }));

  const runCheck = async () => {
    setBusy(true); setCheckErr(null); setResult(null);
    try { setResult(await checkAddress(form)); }
    catch (e) { setCheckErr(e instanceof ApiError ? e.message : t("The check failed.")); }
    finally { setBusy(false); }
  };

  /* ---- Reguli + politici ---- */
  const [rules, setRules] = useState<ValidationRule[]>([]);
  const [meta, setMeta] = useState<PolicyMeta[]>([]);
  const [defaults, setDefaults] = useState<Record<string, unknown>>({});
  const [effective, setEffective] = useState<Record<string, unknown>>({});
  const [dirty, setDirty] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);
  const [saveBusy, setSaveBusy] = useState(false);
  const [loadErr, setLoadErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setLoadErr(null);
    try {
      const [r, p] = await Promise.all([getValidationRules(), getValidationPolicies()]);
      setRules(r.rules); setMeta(r.policy_meta); setDefaults(p.defaults); setEffective(p.effective);
      setDirty({});
    } catch (e) { setLoadErr(e instanceof ApiError ? e.message : t("Could not load the rules.")); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  const current = useMemo(() => ({ ...effective, ...dirty }), [effective, dirty]);
  const isDirty = Object.keys(dirty).length > 0;
  const setPolicy = (key: string, value: unknown) => setDirty((d) => ({ ...d, [key]: value }));

  const save = async () => {
    setSaveBusy(true);
    try {
      // Send the full current view; the backend drops values equal to code defaults.
      const r = await putValidationPolicies(current);
      setEffective(r.effective); setDirty({});
      toast("Politici salvate");
    } catch (e) { toast(e instanceof ApiError ? e.message : t("Saving failed")); }
    finally { setSaveBusy(false); }
  };

  const roRules = rules.filter((r) => r.scope === "RO");
  const intlRules = rules.filter((r) => r.scope === "INTL");

  return (
    <Page
      fullWidth
      title="Address lab"
      subtitle={t("The consolidated validator: check an address, see the rules, tune the policies.")}
      primaryAction={isDirty ? { content: t("Save policies"), onAction: () => void save(), loading: saveBusy } : undefined}
      secondaryActions={[{ content: t("Corrections (CS backlog)"), onAction: () => navigate("/app/cs-queue") }]}
    >
      <BlockStack gap="400">

        {/* ---- Verifică o adresă ---- */}
        <Card>
          <BlockStack gap="300">
            <Text as="h2" variant="headingMd">{t("Check an address")}</Text>
            <InlineStack gap="200" wrap>
              <div style={{ minWidth: 180 }}>
                <Select label={t("Country")} options={COUNTRIES} value={form.country} onChange={set("country")} />
              </div>
              <div style={{ minWidth: 160, flex: "1 1 160px" }}>
                <TextField label={t("County / region")} value={form.province} onChange={set("province")} autoComplete="off" />
              </div>
              <div style={{ minWidth: 180, flex: "1 1 180px" }}>
                <TextField label={t("Locality")} value={form.city} onChange={set("city")} autoComplete="off" />
              </div>
              <div style={{ minWidth: 120 }}>
                <TextField label={t("Postal code")} value={form.zip} onChange={set("zip")} autoComplete="off" />
              </div>
            </InlineStack>
            <InlineStack gap="200" wrap>
              <div style={{ flex: "2 1 300px" }}>
                <TextField label={t("Address 1 (street + number)")} value={form.address1} onChange={set("address1")} autoComplete="off" />
              </div>
              <div style={{ flex: "1 1 180px" }}>
                <TextField label={t("Address 2")} value={form.address2} onChange={set("address2")} autoComplete="off" />
              </div>
              <div style={{ alignSelf: "end" }}>
                <Button variant="primary" loading={busy} onClick={() => void runCheck()}
                  disabled={!form.city && !form.zip && !form.address1}>{t("Check")}</Button>
              </div>
            </InlineStack>

            {checkErr && <Banner tone="critical" title="Eroare" onDismiss={() => setCheckErr(null)}><p>{checkErr}</p></Banner>}

            {result && (
              <Banner tone={verdictTone(result.status)} title={VERDICT_LABEL[result.status] ?? result.status}>
                <BlockStack gap="150">
                  <Text as="p" variant="bodySm">{result.note}</Text>
                  {result.address && (
                    <Text as="p" variant="bodySm" fontWeight="semibold">
                      Corecția: {[result.address.address1, result.address.zip, result.address.city, result.address.province]
                        .filter(Boolean).join(", ")}
                    </Text>
                  )}
                  <Text as="p" variant="bodySm" tone="subdued">{t("source")}: {result.source}</Text>
                </BlockStack>
              </Banner>
            )}
          </BlockStack>
        </Card>

        {loadErr && <Banner tone="critical" title={t("Could not load the rules")} onDismiss={() => setLoadErr(null)}><p>{loadErr}</p></Banner>}

        {loading ? (
          <Card><SkeletonBodyText lines={8} /></Card>
        ) : (
          <>
            {/* ---- Politici (editabile) ---- */}
            <Card>
              <BlockStack gap="300">
                <InlineStack align="space-between" blockAlign="center">
                  <Text as="h2" variant="headingMd">Politici (alegibile)</Text>
                  <Text as="span" variant="bodySm" tone="subdued">
                    Toggle-uri de business — globale, citite de validator la fiecare rulare.
                  </Text>
                </InlineStack>
                <Divider />
                <BlockStack gap="300">
                  {meta.map((m) => {
                    const val = current[m.key];
                    const changed = JSON.stringify(val) !== JSON.stringify(defaults[m.key]);
                    return (
                      <InlineStack key={m.key} gap="300" blockAlign="start" wrap={false}>
                        <div style={{ width: 340, flexShrink: 0 }}>
                          {m.type === "bool" ? (
                            <Checkbox label={m.label} checked={Boolean(val)}
                              onChange={(v) => setPolicy(m.key, v)} />
                          ) : m.type === "enum" ? (
                            <Select label={m.label} value={String(val ?? "")}
                              options={(m.options ?? []).map((o) => ({ label: o, value: o }))}
                              onChange={(v) => setPolicy(m.key, v)} />
                          ) : (
                            <TextField label={m.label} type="number" value={String(val ?? "")}
                              onChange={(v) => setPolicy(m.key, m.type === "int" ? parseInt(v || "0", 10) : parseFloat(v || "0"))}
                              autoComplete="off" />
                          )}
                        </div>
                        <div style={{ flex: 1, paddingTop: m.type === "bool" ? 2 : 24 }}>
                          <InlineStack gap="150" blockAlign="center">
                            <Text as="span" variant="bodySm" tone="subdued">{m.help}</Text>
                            {changed && <Badge tone="attention" size="small">modificat</Badge>}
                          </InlineStack>
                        </div>
                      </InlineStack>
                    );
                  })}
                </BlockStack>
              </BlockStack>
            </Card>

            {/* ---- Reguli de corectitudine (read-only) ---- */}
            <Card>
              <BlockStack gap="300">
                <InlineStack align="space-between" blockAlign="center">
                  <Text as="h2" variant="headingMd">Reguli de corectitudine (mereu active)</Text>
                  <Text as="span" variant="bodySm" tone="subdued">
                    {roRules.length} reguli RO · {intlRules.length} reguli intl — cod, nu se dezactivează din UI.
                  </Text>
                </InlineStack>
                <Divider />
                <InlineStack gap="400" wrap align="start">
                  {[["România", roRules], [t("International (CZ/PL/BG/HU/SK)"), intlRules]].map(([title, list]) => (
                    <div key={title as string} style={{ flex: "1 1 420px", minWidth: 320 }}>
                      <BlockStack gap="200">
                        <Text as="h3" variant="headingSm">{title as string}</Text>
                        {(list as ValidationRule[]).map((r) => (
                          <div key={r.id} style={{ padding: "6px 0", borderBottom: "1px solid var(--p-color-border-subdued)" }}>
                            <Text as="p" variant="bodySm" fontWeight="semibold">{r.name}</Text>
                            <Text as="p" variant="bodySm" tone="subdued">{r.desc}</Text>
                          </div>
                        ))}
                      </BlockStack>
                    </div>
                  ))}
                </InlineStack>
              </BlockStack>
            </Card>
          </>
        )}
      </BlockStack>
    </Page>
  );
}

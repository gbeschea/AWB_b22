import { useCallback, useEffect, useState } from "react";
import {
  BlockStack, Button, Card, Checkbox, InlineGrid, InlineStack, Select, Text, TextField,
} from "@shopify/polaris";
import { getAutomationSchedule, saveAutomationSchedule, type AutomationSchedule, type SpecialRule } from "../lib/api";
import { t } from "../lib/i18n";

function toast(msg: string, isError = false) {
  (window as unknown as { shopify?: { toast: { show: (m: string, o?: { isError: boolean }) => void } } })
    .shopify?.toast.show(msg, isError ? { isError: true } : undefined);
}

type Key = "duplicates" | "parcels" | "surprise" | "blocklist" | "special" | "cod_capture" | "awb";
const SPECIAL_ACTIONS = [
  { label: t("act.hold"), value: "hold" }, { label: t("act.cancel"), value: "cancel" }, { label: t("act.ship"), value: "ship" },
];

const AUTOMATIONS: { key: Key; label: string; help: string; modes: string[] }[] = [
  { key: "duplicates", label: t("auto.duplicates"), help: t("auto.duplicates.help"), modes: ["on_order", "cron", "off"] },
  { key: "parcels", label: t("auto.parcels"), help: t("auto.parcels.help"), modes: ["on_order", "cron", "off"] },
  { key: "surprise", label: t("auto.surprise"), help: t("auto.surprise.help"), modes: ["on_order", "cron", "off"] },
  { key: "blocklist", label: t("auto.blocklist"), help: t("auto.blocklist.help"), modes: ["on_order", "cron", "off"] },
  { key: "special", label: t("auto.special"), help: t("auto.special.help"), modes: ["on_order", "cron", "off"] },
  { key: "cod_capture", label: t("auto.cod_capture"), help: t("auto.cod_capture.help"), modes: ["on_delivered", "cron", "off"] },
  { key: "awb", label: t("auto.awb"), help: t("auto.awb.help"), modes: ["on_order", "cron", "off"] },
];
const MODE_LABEL: Record<string, string> = {
  on_order: t("mode.on_order"), cron: t("mode.cron"), on_delivered: t("mode.on_delivered"), off: t("mode.off"),
};

export function AutomationScheduleCard() {
  const [cfg, setCfg] = useState<AutomationSchedule | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => { getAutomationSchedule().then(setCfg).catch(() => setCfg(null)); }, []);

  const setEntry = (key: Key, patch: Partial<{ mode: string; minutes: number }>) =>
    setCfg((c) => (c ? { ...c, [key]: { ...(c[key] as object), ...patch } } : c));
  const setRules = (fn: (rules: SpecialRule[]) => SpecialRule[]) =>
    setCfg((c) => (c ? { ...c, special_rules: fn(c.special_rules ?? []) } : c));

  const save = useCallback(async () => {
    if (!cfg) return;
    setSaving(true);
    try { await saveAutomationSchedule(cfg); toast(t("sched.saved")); }
    catch (e) { toast(`${t("sched.savefail")}: ${(e as Error).message}`, true); }
    finally { setSaving(false); }
  }, [cfg]);

  if (!cfg) {
    return (
      <Card><BlockStack gap="300">
        <Text as="h2" variant="headingMd">{t("sched.title")}</Text>
        <Text as="p" tone="subdued">{t("sched.loading")}</Text>
      </BlockStack></Card>
    );
  }

  const minutesLabel = (mode: string) =>
    mode === "cron" ? t("sched.every") : mode === "on_order" ? t("sched.after") : "—";
  const minutesDisabled = (mode: string) => mode === "off" || mode === "on_delivered";

  return (
    <Card>
      <BlockStack gap="400">
        <BlockStack gap="100">
          <InlineStack align="space-between" blockAlign="center">
            <Text as="h2" variant="headingMd">{t("sched.title")}</Text>
            <Button variant="primary" loading={saving} onClick={() => void save()}>{t("sched.save")}</Button>
          </InlineStack>
          <Text as="p" tone="subdued">
            {t("sched.desc")}
          </Text>
        </BlockStack>

        {AUTOMATIONS.map((a) => {
          const entry = cfg[a.key] as { mode: string; minutes: number };
          return (
            <InlineGrid key={a.key} columns={{ xs: 1, sm: "2fr 1fr 200px" }} gap="300"
              alignItems="center">
              <BlockStack gap="050">
                <Text as="span" variant="bodyMd" fontWeight="semibold">{a.label}</Text>
                <Text as="span" tone="subdued" variant="bodySm">{a.help}</Text>
              </BlockStack>
              <Select label={t("sched.mode")} labelHidden
                options={a.modes.map((m) => ({ label: MODE_LABEL[m], value: m }))}
                value={entry.mode} onChange={(v) => setEntry(a.key, { mode: v })} />
              {/* Câmpul de minute apare DOAR unde are sens. Înainte rămânea vizibil și dezactivat,
                  cu eticheta „—" și un 0 mort (ex. „la livrare"), și ocupa o treime din rând
                  pentru un număr de o cifră. */}
              {minutesDisabled(entry.mode) ? <div /> : (
                <div style={{ width: 148 }}>
                  <TextField label={minutesLabel(entry.mode)} type="number" autoComplete="off"
                    suffix={t("sched.min")}
                    value={String(entry.minutes ?? 0)}
                    onChange={(v) => setEntry(a.key, { minutes: Number(v) || 0 })} />
                </div>
              )}
            </InlineGrid>
          );
        })}

        <BlockStack gap="100">
          <Text as="h3" variant="headingSm">{t("nohold.title")}</Text>
          <Checkbox
            label={t("nohold.label")}
            checked={!!cfg.no_hold}
            onChange={(v) => setCfg((c) => (c ? { ...c, no_hold: v } : c))}
            helpText={t("nohold.help")}
          />
        </BlockStack>

        <BlockStack gap="100">
          <Text as="h3" variant="headingSm">{t("fulfill.title")}</Text>
          <Select
            label={t("fulfill.title")} labelHidden
            options={[
              { label: t("fulfill.on_pickup"), value: "on_pickup" },
              { label: t("fulfill.on_label"), value: "on_label" },
            ]}
            value={cfg.fulfill_when ?? "on_pickup"}
            onChange={(v) => setCfg((c) => (c ? { ...c, fulfill_when: v } : c))}
            helpText={t("fulfill.help")}
          />
        </BlockStack>

        <BlockStack gap="100">
          <Text as="h3" variant="headingSm">{t("rules.title")}</Text>
          <Text as="p" tone="subdued" variant="bodySm">
            {t("rules.desc")}
          </Text>
          {(cfg.special_rules ?? []).map((r, i) => (
            <InlineGrid key={i} columns={{ xs: 1, sm: 3 }} gap="300">
              <TextField label={t("rules.contains")} labelHidden autoComplete="off" placeholder="ex. influencer"
                value={r.contains}
                onChange={(v) => setRules((rs) => rs.map((x, j) => (j === i ? { ...x, contains: v } : x)))} />
              <Select label={t("rules.action")} labelHidden options={SPECIAL_ACTIONS} value={r.action}
                onChange={(v) => setRules((rs) => rs.map((x, j) => (j === i ? { ...x, action: v } : x)))} />
              <Button variant="tertiary" tone="critical"
                onClick={() => setRules((rs) => rs.filter((_, j) => j !== i))}>{t("rules.remove")}</Button>
            </InlineGrid>
          ))}
          <InlineStack>
            <Button onClick={() => setRules((rs) => [...rs, { contains: "", action: "hold" }])}>{t("rules.add")}</Button>
          </InlineStack>
        </BlockStack>
      </BlockStack>
    </Card>
  );
}

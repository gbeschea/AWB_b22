import { useCallback, useEffect, useState } from "react";
import {
  BlockStack, Button, Card, InlineGrid, InlineStack, Select, Text, TextField,
} from "@shopify/polaris";
import { getAutomationSchedule, saveAutomationSchedule, type AutomationSchedule } from "../lib/api";

function toast(msg: string, isError = false) {
  (window as unknown as { shopify?: { toast: { show: (m: string, o?: { isError: boolean }) => void } } })
    .shopify?.toast.show(msg, isError ? { isError: true } : undefined);
}

type Key = "duplicates" | "parcels" | "surprise" | "blocklist" | "risk" | "cod_capture" | "awb";

const AUTOMATIONS: { key: Key; label: string; help: string; modes: string[] }[] = [
  { key: "duplicates", label: "Duplicate", help: "Comenzi dublate ale aceluiași client — la nivel de organizație.", modes: ["on_order", "cron", "off"] },
  { key: "parcels", label: "Nr. colete", help: "Câte colete are AWB-ul (memorat pentru AWB).", modes: ["on_order", "cron", "off"] },
  { key: "surprise", label: "Surpriză (parfum)", help: "Doar Esteban / George Talent / Lab Noir / Nubra.", modes: ["on_order", "cron", "off"] },
  { key: "blocklist", label: "Blocklist", help: "Client blocat manual sau serial-refuser.", modes: ["on_order", "cron", "off"] },
  { key: "risk", label: "Risc comandă", help: "Scor pe profilul clientului (istoric refuzuri) — organizație.", modes: ["on_order", "cron", "off"] },
  { key: "cod_capture", label: "COD capture", help: "La livrare: marchează plătit / tag refuzat.", modes: ["on_delivered", "cron", "off"] },
  { key: "awb", label: "AWB", help: "Creează eticheta (doar în fereastra AWB).", modes: ["on_order", "cron", "off"] },
];
const MODE_LABEL: Record<string, string> = {
  on_order: "La comandă", cron: "Cron (periodic)", on_delivered: "La livrare", off: "Oprit",
};
const ACTIONS = [
  { label: "Nimic", value: "none" }, { label: "Hold", value: "hold" }, { label: "Anulare", value: "cancel" },
];

export function AutomationScheduleCard() {
  const [cfg, setCfg] = useState<AutomationSchedule | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => { getAutomationSchedule().then(setCfg).catch(() => setCfg(null)); }, []);

  const setEntry = (key: Key, patch: Partial<{ mode: string; minutes: number }>) =>
    setCfg((c) => (c ? { ...c, [key]: { ...(c[key] as object), ...patch } } : c));
  const setRisk = (lvl: "medium" | "high", v: string) =>
    setCfg((c) => (c ? { ...c, risk_actions: { ...c.risk_actions, [lvl]: v } } : c));

  const save = useCallback(async () => {
    if (!cfg) return;
    setSaving(true);
    try { await saveAutomationSchedule(cfg); toast("Programare salvată"); }
    catch (e) { toast(`Nu s-a putut salva: ${(e as Error).message}`, true); }
    finally { setSaving(false); }
  }, [cfg]);

  if (!cfg) {
    return (
      <Card><BlockStack gap="300">
        <Text as="h2" variant="headingMd">Programare automatizări</Text>
        <Text as="p" tone="subdued">Se încarcă…</Text>
      </BlockStack></Card>
    );
  }

  const minutesLabel = (mode: string) =>
    mode === "cron" ? "Interval (min)" : mode === "on_order" ? "Min. după comandă" : "—";
  const minutesDisabled = (mode: string) => mode === "off" || mode === "on_delivered";

  return (
    <Card>
      <BlockStack gap="400">
        <BlockStack gap="100">
          <InlineStack align="space-between" blockAlign="center">
            <Text as="h2" variant="headingMd">Programare automatizări</Text>
            <Button variant="primary" loading={saving} onClick={() => void save()}>Salvează</Button>
          </InlineStack>
          <Text as="p" tone="subdued">
            Alege CÂND rulează fiecare automatizare: la comandă (imediat sau după X min), periodic (cron), sau la livrare (COD). Totul rulează în modul shadow (log-only) până la go-live.
          </Text>
        </BlockStack>

        {AUTOMATIONS.map((a) => {
          const entry = cfg[a.key] as { mode: string; minutes: number };
          return (
            <InlineGrid key={a.key} columns={{ xs: 1, sm: 3 }} gap="300">
              <BlockStack gap="050">
                <Text as="span" variant="bodyMd" fontWeight="semibold">{a.label}</Text>
                <Text as="span" tone="subdued" variant="bodySm">{a.help}</Text>
              </BlockStack>
              <Select label="Mod" labelHidden
                options={a.modes.map((m) => ({ label: MODE_LABEL[m], value: m }))}
                value={entry.mode} onChange={(v) => setEntry(a.key, { mode: v })} />
              <TextField label={minutesLabel(entry.mode)} type="number" autoComplete="off"
                value={String(entry.minutes ?? 0)} disabled={minutesDisabled(entry.mode)}
                onChange={(v) => setEntry(a.key, { minutes: Number(v) || 0 })} />
            </InlineGrid>
          );
        })}

        <BlockStack gap="100">
          <Text as="h3" variant="headingSm">Acțiuni la risc de comandă</Text>
          <Text as="p" tone="subdued" variant="bodySm">Ce se întâmplă la fiecare nivel de risc (pe profilul clientului).</Text>
          <InlineGrid columns={{ xs: 1, sm: 2 }} gap="300">
            <Select label="Risc mediu" options={ACTIONS} value={cfg.risk_actions.medium}
              onChange={(v) => setRisk("medium", v)} />
            <Select label="Risc mare" options={ACTIONS} value={cfg.risk_actions.high}
              onChange={(v) => setRisk("high", v)} />
          </InlineGrid>
        </BlockStack>
      </BlockStack>
    </Card>
  );
}

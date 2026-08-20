import { useCallback, useEffect, useState } from "react";
import { BlockStack, Button, Card, Checkbox, InlineStack, Text, TextField, Banner } from "@shopify/polaris";
import { getInventoryGuard, saveInventoryGuard, runInventoryGuard, type InventoryGuard } from "../lib/api";

type Cfg = InventoryGuard;

function toast(msg: string, isError = false) {
  (window as unknown as { shopify?: { toast: { show: (m: string, o?: { isError: boolean }) => void } } })
    .shopify?.toast.show(msg, isError ? { isError: true } : undefined);
}

export function InventoryGuardCard() {
  const [cfg, setCfg] = useState<Cfg | null>(null);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);

  useEffect(() => { getInventoryGuard().then(setCfg).catch(() => setCfg(null)); }, []);

  const save = useCallback(async () => {
    if (!cfg) return;
    setSaving(true);
    try { await saveInventoryGuard(cfg); toast("Setările gărzii de stoc au fost salvate."); }
    catch (e) { toast(`Nu s-a putut salva: ${(e as Error).message}`, true); }
    finally { setSaving(false); }
  }, [cfg]);

  const runNow = useCallback(async () => {
    setRunning(true);
    try {
      const r = await runInventoryGuard();
      toast(r.baseline
        ? `Linie de bază: ${r.baseline} produse deja sub prag, înregistrate fără email.`
        : `Verificate ${r.checked ?? 0} produse · ${r.low ?? 0} sub prag · ${r.new_alerts ?? 0} alerte noi.`);
    } catch (e) { toast(`Rulare eșuată: ${(e as Error).message}`, true); }
    finally { setRunning(false); }
  }, []);

  if (!cfg) return null;

  return (
    <Card>
      <BlockStack gap="400">
        <InlineStack align="space-between" blockAlign="center">
          <Text as="h2" variant="headingMd">Gardă de stoc</Text>
          <InlineStack gap="200">
            <Button loading={running} onClick={() => void runNow()}>Verifică acum</Button>
            <Button variant="primary" loading={saving} onClick={() => void save()}>Salvează</Button>
          </InlineStack>
        </InlineStack>

        <Text as="p" tone="subdued">
          Trimite un email când stocul TOTAL al unui produs scade sub prag. Stocul e suma pe toate
          magazinele Shopify — Trendyol nu intră în calcul, iar produsele draft sunt sărite.
          Primești un singur mail per produs, la trecerea sub prag.
        </Text>

        {cfg.smtp_ready === false && (
          <Banner tone="warning"><p>SMTP nu e configurat pe server — alertele nu pot pleca.</p></Banner>
        )}

        <Checkbox label="Alerte pornite" checked={!!cfg.enabled}
          onChange={(v) => setCfg({ ...cfg, enabled: v })} />

        <InlineStack gap="300">
          <TextField label="Prag (bucăți)" type="number" autoComplete="off"
            value={String(cfg.threshold ?? 50)}
            onChange={(v) => setCfg({ ...cfg, threshold: Number(v) || 0 })} />
          <TextField label="Marjă de re-armare (%)" type="number" autoComplete="off"
            value={String(cfg.hysteresis_pct ?? 20)}
            onChange={(v) => setCfg({ ...cfg, hysteresis_pct: Number(v) || 0 })}
            helpText="Alertă nouă doar după ce stocul urcă peste prag + marja asta." />
        </InlineStack>

        <TextField label="Destinatari" autoComplete="off" multiline={2}
          placeholder="achizitii@arona.ro, depozit@arona.ro"
          value={(cfg.recipients || []).join(", ")}
          onChange={(v) => setCfg({ ...cfg, recipients: v.split(",").map((x) => x.trim()).filter(Boolean) })}
          helpText="Adrese separate prin virgulă." />
      </BlockStack>
    </Card>
  );
}

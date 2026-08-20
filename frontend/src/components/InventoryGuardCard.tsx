import { useCallback, useEffect, useState } from "react";
import { BlockStack, Button, Card, Checkbox, InlineGrid, InlineStack, Select, Text, TextField, Banner } from "@shopify/polaris";
import { getInventoryGuard, saveInventoryGuard, runInventoryGuard, getOverviewStores,
         type InventoryGuard, type InventoryRule, type InventoryCategory } from "../lib/api";

type Cfg = InventoryGuard;

function toast(msg: string, isError = false) {
  (window as unknown as { shopify?: { toast: { show: (m: string, o?: { isError: boolean }) => void } } })
    .shopify?.toast.show(msg, isError ? { isError: true } : undefined);
}

export function InventoryGuardCard() {
  const [cfg, setCfg] = useState<Cfg | null>(null);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const [stores, setStores] = useState<string[]>([]);

  const [loadError, setLoadError] = useState<string | null>(null);
  useEffect(() => {
    getInventoryGuard().then(setCfg).catch((e) => setLoadError((e as Error).message || "eroare"));
  }, []);
  useEffect(() => {
    getOverviewStores()
      .then((r) => setStores((r as unknown as { stores?: { name?: string; domain?: string }[] })
        .stores?.map((s) => s.name || s.domain || "").filter(Boolean) ?? []))
      .catch(() => setStores([]));
  }, []);

  const setRules = (fn: (rs: InventoryRule[]) => InventoryRule[]) =>
    setCfg((c) => (c ? { ...c, rules: fn(c.rules ?? []) } : c));
  const setCats = (fn: (cs: InventoryCategory[]) => InventoryCategory[]) =>
    setCfg((c) => (c ? { ...c, categories: fn(c.categories ?? []) } : c));

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

  // Fără asta, cardul DISPĂREA în tăcere dacă apelul eșua — te uitai în Settings și pur și simplu
  // nu era acolo, fără niciun indiciu de ce.
  if (!cfg) {
    return (
      <Card><BlockStack gap="200">
        <Text as="h2" variant="headingMd">Gardă de stoc</Text>
        <Text as="p" tone={loadError ? "critical" : "subdued"}>
          {loadError ? `Nu s-au putut încărca setările: ${loadError}` : "Se încarcă…"}
        </Text>
      </BlockStack></Card>
    );
  }

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

        <BlockStack gap="200">
          <Text as="h3" variant="headingSm">Categorii</Text>
          <Text as="p" tone="subdued" variant="bodySm">
            Un grup cu nume, ca să scrii o singură regulă pentru el (ex. „parfumuri").
            Poți alege magazine, produse anume, sau amândouă.
          </Text>
          {(cfg.categories ?? []).map((c, i) => (
            <InlineGrid key={i} columns={{ xs: 1, sm: "1fr 1.6fr 1.6fr auto" }} gap="200">
              <TextField label="Nume" labelHidden autoComplete="off" placeholder="nume (ex. parfumuri)"
                value={c.name}
                onChange={(v) => setCats((cs) => cs.map((x, j) => (j === i ? { ...x, name: v } : x)))} />
              <TextField label="Magazine" labelHidden autoComplete="off"
                placeholder="magazine, separate prin virgulă"
                value={(c.stores || []).join(", ")}
                onChange={(v) => setCats((cs) => cs.map((x, j) => (j === i
                  ? { ...x, stores: v.split(",").map((e) => e.trim()).filter(Boolean) } : x)))} />
              <TextField label="Produse" labelHidden autoComplete="off"
                placeholder="SKU-uri anume, separate prin virgulă (opțional)"
                value={(c.skus || []).join(", ")}
                onChange={(v) => setCats((cs) => cs.map((x, j) => (j === i
                  ? { ...x, skus: v.split(",").map((e) => e.trim()).filter(Boolean) } : x)))} />
              <Button variant="tertiary" tone="critical"
                onClick={() => setCats((cs) => cs.filter((_, j) => j !== i))}>Șterge</Button>
            </InlineGrid>
          ))}
          <InlineStack>
            <Button onClick={() => setCats((cs) => [...cs, { name: "", stores: [], skus: [] }])}>
              Adaugă categorie
            </Button>
          </InlineStack>
        </BlockStack>

        <BlockStack gap="200">
          <Text as="h3" variant="headingSm">Reguli speciale</Text>
          <Text as="p" tone="subdued" variant="bodySm">
            Peste pragul general. Alege UN nivel de măsurare: fără magazin/categorie = stocul TOTAL
            din grup · categorie = suma magazinelor din ea · magazin = stocul acelui magazin.
            Poți lipi oricâte SKU-uri într-o regulă — toate primesc același prag. Gol = toate produsele. „În plus la" = cine primește pe lângă destinatarii
            generali, doar pentru ce prinde regula asta.
          </Text>
          {(cfg.rules ?? []).map((r, i) => (
            <InlineGrid key={i} columns={{ xs: 1, sm: "1.1fr 1.1fr 2fr 0.6fr 1.5fr auto" }} gap="200">
              <Select label="Magazin" labelHidden
                options={[{ label: "— magazin —", value: "" },
                          ...stores.map((s) => ({ label: s, value: s }))]}
                value={r.store}
                onChange={(v) => setRules((rs) => rs.map((x, j) =>
                  (j === i ? { ...x, store: v, category: v ? "" : x.category } : x)))} />
              <Select label="Categorie" labelHidden
                options={[{ label: "— categorie —", value: "" },
                          ...(cfg.categories ?? []).map((c) => ({ label: c.name, value: c.name }))]}
                value={r.category}
                onChange={(v) => setRules((rs) => rs.map((x, j) =>
                  (j === i ? { ...x, category: v, store: v ? "" : x.store } : x)))} />
              <TextField label="Produse" labelHidden autoComplete="off" multiline={1}
                placeholder="SKU-uri (lipește oricâte, separate prin virgulă) — gol = toate"
                value={(r.skus || []).join(", ")}
                onChange={(v) => setRules((rs) => rs.map((x, j) => (j === i
                  ? { ...x, skus: v.split(/[,;\n]/).map((e) => e.trim()).filter(Boolean) } : x)))} />
              <TextField label="Prag" labelHidden type="number" autoComplete="off" placeholder="prag"
                value={String(r.threshold)}
                onChange={(v) => setRules((rs) => rs.map((x, j) => (j === i ? { ...x, threshold: Number(v) || 0 } : x)))} />
              <TextField label="În plus la" labelHidden autoComplete="off" placeholder="email-uri în plus"
                value={(r.recipients || []).join(", ")}
                onChange={(v) => setRules((rs) => rs.map((x, j) => (j === i
                  ? { ...x, recipients: v.split(",").map((e) => e.trim()).filter(Boolean) } : x)))} />
              <Button variant="tertiary" tone="critical"
                onClick={() => setRules((rs) => rs.filter((_, j) => j !== i))}>Șterge</Button>
            </InlineGrid>
          ))}
          <InlineStack>
            <Button onClick={() => setRules((rs) => [...rs, { store: "", category: "", skus: [], threshold: cfg.threshold ?? 50, recipients: [] }])}>
              Adaugă regulă
            </Button>
          </InlineStack>
        </BlockStack>

        <TextField label="Destinatari" autoComplete="off" multiline={2}
          placeholder="achizitii@arona.ro, depozit@arona.ro"
          value={(cfg.recipients || []).join(", ")}
          onChange={(v) => setCfg({ ...cfg, recipients: v.split(",").map((x) => x.trim()).filter(Boolean) })}
          helpText="Adrese separate prin virgulă." />
      </BlockStack>
    </Card>
  );
}

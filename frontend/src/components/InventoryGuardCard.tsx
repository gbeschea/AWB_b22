import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Badge, Banner, BlockStack, Box, Button, Card, ChoiceList, Divider, InlineGrid, InlineStack,
  Select, Text, TextField,
} from "@shopify/polaris";
import {
  getInventoryGuard, saveInventoryGuard, runInventoryGuard, getOverviewStores,
  type InventoryGuard, type InventoryCategory,
} from "../lib/api";

/** O excepție. „Exclus de la gardă" NU e un concept separat — e o excepție cu pragul „niciodată".
 *  Backend-ul le ține în două liste fiindcă le evaluează diferit; interfața le arată ca una
 *  singură și le desparte abia la salvare. */
type Exception = {
  kind: "alert" | "ignore";
  store: string; category: string; skus: string[];
  threshold: number; recipients: string[]; exclude_recipients: string[];
};

function toast(msg: string, isError = false) {
  (window as unknown as { shopify?: { toast: { show: (m: string, o?: { isError: boolean }) => void } } })
    .shopify?.toast.show(msg, isError ? { isError: true } : undefined);
}

const csv = (v: string) => v.split(/[,;\n]/).map((x) => x.trim()).filter(Boolean);
const shortMail = (e: string) => e.split("@")[0];

function fromCfg(cfg: InventoryGuard): Exception[] {
  return [
    ...(cfg.rules ?? []).map((r): Exception => ({
      kind: "alert", store: r.store || "", category: r.category || "", skus: r.skus || [],
      threshold: r.threshold, recipients: r.recipients || [],
      exclude_recipients: r.exclude_recipients || [],
    })),
    ...(cfg.exclusions ?? []).map((e): Exception => ({
      kind: "ignore", store: e.store || "", category: e.category || "", skus: e.skus || [],
      threshold: 0, recipients: [], exclude_recipients: [],
    })),
  ];
}

function toCfg(cfg: InventoryGuard, list: Exception[]): InventoryGuard {
  return {
    ...cfg,
    rules: list.filter((x) => x.kind === "alert").map((x) => ({
      store: x.store, category: x.category, skus: x.skus, threshold: x.threshold,
      recipients: x.recipients, exclude_recipients: x.exclude_recipients,
    })),
    exclusions: list.filter((x) => x.kind === "ignore").map((x) => ({
      store: x.store, category: x.category, skus: x.skus,
    })),
  };
}

/** Rândul închis trebuie să spună TOT ce face regula, într-o propoziție. Altfel „ce reguli am pus?"
 *  se răspunde doar deschizând fiecare formular pe rând. */
function summary(x: Exception) {
  const target = x.store || x.category || "Tot grupul";
  const what = x.skus.length
    ? (x.skus.length <= 2 ? x.skus.join(", ").toUpperCase() : `${x.skus.length} produse`)
    : "toate produsele";
  return { target, what };
}

export function InventoryGuardCard() {
  const [cfg, setCfg] = useState<InventoryGuard | null>(null);
  const [list, setList] = useState<Exception[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const [stores, setStores] = useState<string[]>([]);
  const [open, setOpen] = useState<number | null>(null);          // ce excepție e deschisă
  const [openCat, setOpenCat] = useState<number | null>(null);
  const [showGroups, setShowGroups] = useState(false);
  const [editDefault, setEditDefault] = useState(false);

  useEffect(() => {
    getInventoryGuard()
      .then((c) => { setCfg(c); setList(fromCfg(c)); })
      .catch((e) => setLoadError((e as Error).message || "eroare"));
  }, []);
  useEffect(() => {
    getOverviewStores()
      .then((r) => setStores((r as unknown as { stores?: { name?: string; domain?: string }[] })
        .stores?.map((s) => s.name || s.domain || "").filter(Boolean) ?? []))
      .catch(() => setStores([]));
  }, []);

  const cats = cfg?.categories ?? [];
  const setCats = (fn: (cs: InventoryCategory[]) => InventoryCategory[]) =>
    setCfg((c) => (c ? { ...c, categories: fn(c.categories ?? []) } : c));
  const patch = (i: number, p: Partial<Exception>) =>
    setList((xs) => xs.map((x, j) => (j === i ? { ...x, ...p } : x)));

  // UN singur selector de țintă: două selectoare care se exclud reciproc te pun să ghicești ce se
  // întâmplă dacă alegi în amândouă.
  const targetOptions = useMemo(() => [
    { label: "Tot grupul (stoc total)", value: "" },
    ...(cats.filter((c) => c.name).length
      ? [{ title: "Grupuri", options: cats.filter((c) => c.name)
          .map((c) => ({ label: c.name, value: "c:" + c.name })) }] : []),
    { title: "Magazine", options: stores.map((s) => ({ label: s, value: "s:" + s })) },
  ], [cats, stores]);
  const targetValue = (x: { store: string; category: string }) =>
    x.store ? "s:" + x.store : x.category ? "c:" + x.category : "";
  const applyTarget = (v: string) =>
    v.startsWith("s:") ? { store: v.slice(2), category: "" }
      : v.startsWith("c:") ? { store: "", category: v.slice(2) }
        : { store: "", category: "" };

  const save = useCallback(async () => {
    if (!cfg) return;
    setSaving(true);
    try { await saveInventoryGuard(toCfg(cfg, list)); toast("Setările gărzii de stoc au fost salvate."); }
    catch (e) { toast(`Nu s-a putut salva: ${(e as Error).message}`, true); }
    finally { setSaving(false); }
  }, [cfg, list]);

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
        <InlineStack align="space-between" blockAlign="center" gap="300">
          <InlineStack gap="200" blockAlign="center">
            <Text as="h2" variant="headingMd">Gardă de stoc</Text>
            {cfg.enabled ? <Badge tone="success">Pornită</Badge> : <Badge>Oprită</Badge>}
          </InlineStack>
          <InlineStack gap="200">
            <Button loading={running} onClick={() => void runNow()}>Verifică acum</Button>
            <Button variant="primary" loading={saving} onClick={() => void save()}>Salvează</Button>
          </InlineStack>
        </InlineStack>

        {cfg.smtp_ready === false && (
          <Banner tone="warning"><p>SMTP nu e configurat pe server — alertele nu pot pleca.</p></Banner>
        )}

        {/* ── Implicit ─────────────────────────────────────────────────────────────── */}
        <Box background="bg-surface-secondary" borderRadius="200" padding="300">
          <BlockStack gap="300">
            <InlineStack align="space-between" blockAlign="center" gap="200" wrap={false}>
              <Text as="span" variant="bodyMd">
                <b>Implicit:</b> alertează sub <b>{cfg.threshold ?? 50}</b> buc
                {(cfg.recipients || []).length
                  ? <> · trimite la <b>{(cfg.recipients || []).map(shortMail).join(", ")}</b></>
                  : <Text as="span" tone="critical"> · fără destinatari</Text>}
              </Text>
              <Button variant="tertiary" onClick={() => setEditDefault((v) => !v)}>
                {editDefault ? "Gata" : "Editează"}
              </Button>
            </InlineStack>
            {editDefault && (
              <InlineGrid columns={{ xs: 1, md: "140px 150px 1fr" }} gap="300">
                <Select label="Alerte" value={cfg.enabled ? "1" : "0"}
                  options={[{ label: "Pornite", value: "1" }, { label: "Oprite", value: "0" }]}
                  onChange={(v) => setCfg({ ...cfg, enabled: v === "1" })} />
                <TextField label="Alertează sub" type="number" autoComplete="off" suffix="buc"
                  value={String(cfg.threshold ?? 50)}
                  onChange={(v) => setCfg({ ...cfg, threshold: Number(v) || 0 })} />
                <TextField label="Trimite la" autoComplete="off" placeholder="nume@arona.ro"
                  value={(cfg.recipients || []).join(", ")}
                  onChange={(v) => setCfg({ ...cfg, recipients: csv(v) })} />
                <Box>
                  <TextField label="Marjă de re-armare" type="number" autoComplete="off" suffix="%"
                    value={String(cfg.hysteresis_pct ?? 20)}
                    onChange={(v) => setCfg({ ...cfg, hysteresis_pct: Number(v) || 0 })}
                    helpText="Alertă nouă doar după ce urcă peste prag + marja asta." />
                </Box>
              </InlineGrid>
            )}
          </BlockStack>
        </Box>

        {/* ── Excepții ─────────────────────────────────────────────────────────────── */}
        <BlockStack gap="200">
          <InlineStack align="space-between" blockAlign="center">
            <Text as="h3" variant="headingSm">{`Excepții (${list.length})`}</Text>
            <InlineStack gap="200">
              <Button variant="tertiary" onClick={() => setShowGroups((v) => !v)}>
                {`Grupuri (${cats.length})`}
              </Button>
              <Button onClick={() => { setList((xs) => [...xs, {
                kind: "alert", store: "", category: "", skus: [],
                threshold: cfg.threshold ?? 50, recipients: [], exclude_recipients: [],
              }]); setOpen(list.length); }}>Adaugă</Button>
            </InlineStack>
          </InlineStack>

          {list.length === 0 && (
            <Text as="p" tone="subdued" variant="bodySm">
              Nicio excepție — toate produsele merg pe regula implicită.
            </Text>
          )}

          {list.map((x, i) => {
            const { target, what } = summary(x);
            const isOpen = open === i;
            return (
              <Box key={i} borderWidth="025" borderColor={isOpen ? "border-emphasis" : "border"}
                borderRadius="200" padding="300">
                <BlockStack gap={isOpen ? "300" : "0"}>
                  <InlineStack align="space-between" blockAlign="center" gap="200" wrap={false}>
                    <InlineStack gap="200" blockAlign="center" wrap={false}>
                      <Text as="span" variant="bodyMd" fontWeight="semibold">{target}</Text>
                      <Text as="span" tone="subdued" variant="bodySm">{what}</Text>
                      {x.kind === "ignore"
                        ? <Badge tone="warning">fără alerte</Badge>
                        : <Badge tone="attention">{`sub ${x.threshold} buc`}</Badge>}
                      {x.kind === "alert" && x.recipients.length > 0 && (
                        <Text as="span" tone="subdued" variant="bodySm">
                          {"+ " + x.recipients.map(shortMail).join(", ")}
                        </Text>
                      )}
                      {x.kind === "alert" && x.exclude_recipients.length > 0 && (
                        <Text as="span" tone="subdued" variant="bodySm">
                          {"− " + x.exclude_recipients.map(shortMail).join(", ")}
                        </Text>
                      )}
                    </InlineStack>
                    <InlineStack gap="100">
                      <Button variant="tertiary" onClick={() => setOpen(isOpen ? null : i)}>
                        {isOpen ? "Gata" : "Editează"}
                      </Button>
                      <Button variant="tertiary" tone="critical"
                        onClick={() => { setList((xs) => xs.filter((_, j) => j !== i)); setOpen(null); }}>
                        Șterge
                      </Button>
                    </InlineStack>
                  </InlineStack>

                  {isOpen && (
                    <BlockStack gap="300">
                      <Divider />
                      <InlineGrid columns={{ xs: 1, md: "1.3fr 1.4fr 170px 120px" }} gap="300">
                        <Select label="Se aplică la" options={targetOptions} value={targetValue(x)}
                          helpText={x.store ? "Stocul acestui magazin."
                            : x.category ? "Suma magazinelor din grup." : "Stocul total din grup."}
                          onChange={(v) => patch(i, applyTarget(v))} />
                        <TextField label="Produse (SKU)" autoComplete="off"
                          placeholder="toate, dacă e gol" helpText="Lipește oricâte, separate prin virgulă."
                          value={x.skus.join(", ")} onChange={(v) => patch(i, { skus: csv(v) })} />
                        <Select label="Ce facem" value={x.kind}
                          options={[{ label: "Alertează sub…", value: "alert" },
                                    { label: "Nu alerta deloc", value: "ignore" }]}
                          onChange={(v) => patch(i, { kind: v as Exception["kind"] })} />
                        {x.kind === "alert"
                          ? <TextField label="Prag" type="number" autoComplete="off" suffix="buc"
                              value={String(x.threshold)}
                              onChange={(v) => patch(i, { threshold: Number(v) || 0 })} />
                          : <div />}
                      </InlineGrid>
                      {x.kind === "alert" && (
                        <InlineGrid columns={{ xs: 1, md: "1fr 1fr" }} gap="300">
                          <TextField label="Primesc în plus" autoComplete="off" placeholder="cineva@arona.ro"
                            value={x.recipients.join(", ")}
                            onChange={(v) => patch(i, { recipients: csv(v) })} />
                          <TextField label="Nu primesc" autoComplete="off" placeholder="cineva@arona.ro"
                            helpText="Chiar dacă e în lista implicită."
                            value={x.exclude_recipients.join(", ")}
                            onChange={(v) => patch(i, { exclude_recipients: csv(v) })} />
                        </InlineGrid>
                      )}
                    </BlockStack>
                  )}
                </BlockStack>
              </Box>
            );
          })}
        </BlockStack>

        {/* ── Grupuri ──────────────────────────────────────────────────────────────── */}
        {showGroups && (
          <BlockStack gap="200">
            <Divider />
            <InlineStack align="space-between" blockAlign="center">
              <BlockStack gap="050">
                <Text as="h3" variant="headingSm">Grupuri de magazine</Text>
                <Text as="p" tone="subdued" variant="bodySm">
                  O singură excepție pentru mai multe magazine. Stocul grupului = suma lor.
                </Text>
              </BlockStack>
              <Button onClick={() => { setCats((cs) => [...cs, { name: "", stores: [], skus: [] }]);
                setOpenCat(cats.length); }}>Adaugă grup</Button>
            </InlineStack>

            {cats.map((c, i) => {
              const isOpen = openCat === i;
              return (
                <Box key={i} borderWidth="025" borderColor={isOpen ? "border-emphasis" : "border"}
                  borderRadius="200" padding="300">
                  <BlockStack gap={isOpen ? "300" : "0"}>
                    <InlineStack align="space-between" blockAlign="center" wrap={false}>
                      <InlineStack gap="200" blockAlign="center">
                        <Text as="span" variant="bodyMd" fontWeight="semibold">
                          {c.name || "(fără nume)"}
                        </Text>
                        <Text as="span" tone="subdued" variant="bodySm">
                          {(c.stores || []).length
                            ? `${(c.stores || []).length} magazine`
                            : "niciun magazin"}
                          {(c.skus || []).length ? ` · ${(c.skus || []).length} produse` : ""}
                        </Text>
                      </InlineStack>
                      <InlineStack gap="100">
                        <Button variant="tertiary" onClick={() => setOpenCat(isOpen ? null : i)}>
                          {isOpen ? "Gata" : "Editează"}
                        </Button>
                        <Button variant="tertiary" tone="critical"
                          onClick={() => { setCats((cs) => cs.filter((_, j) => j !== i)); setOpenCat(null); }}>
                          Șterge
                        </Button>
                      </InlineStack>
                    </InlineStack>

                    {isOpen && (
                      <BlockStack gap="300">
                        <Divider />
                        <InlineGrid columns={{ xs: 1, md: "240px 1fr" }} gap="300">
                          <BlockStack gap="300">
                            <TextField label="Nume" autoComplete="off" placeholder="parfumuri"
                              value={c.name}
                              onChange={(v) => setCats((cs) => cs.map((x, j) => (j === i ? { ...x, name: v } : x)))} />
                            <TextField label="Doar produsele (SKU)" autoComplete="off"
                              placeholder="toate, dacă e gol" value={(c.skus || []).join(", ")}
                              onChange={(v) => setCats((cs) => cs.map((x, j) => (j === i ? { ...x, skus: csv(v) } : x)))} />
                          </BlockStack>
                          {/* Bifat, nu scris: numele au apostrofuri și diacritice („Maison d'Esteban"),
                              iar o literă greșită face grupul să nu prindă nimic — tăcut. */}
                          <div style={{ maxHeight: 200, overflowY: "auto",
                            border: "1px solid var(--p-color-border)", borderRadius: 8, padding: 12 }}>
                            <ChoiceList allowMultiple title="Magazine în grup"
                              choices={stores.map((s) => ({ label: s, value: s }))}
                              selected={c.stores || []}
                              onChange={(sel) => setCats((cs) => cs.map((x, j) => (j === i ? { ...x, stores: sel } : x)))} />
                          </div>
                        </InlineGrid>
                      </BlockStack>
                    )}
                  </BlockStack>
                </Box>
              );
            })}
          </BlockStack>
        )}
      </BlockStack>
    </Card>
  );
}

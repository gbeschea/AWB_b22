import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Badge, Banner, BlockStack, Box, Button, Card, ChoiceList, Divider, InlineGrid, InlineStack,
  Select, Text, TextField,
} from "@shopify/polaris";
import {
  getInventoryGuard, saveInventoryGuard, runInventoryGuard, getOverviewStores,
  type InventoryGuard,
} from "../lib/api";
import { t } from "../lib/i18n";

/** O excepție. „Exclus de la gardă" NU e un concept separat — e o excepție cu pragul „niciodată".
 *  Backend-ul le ține în două liste fiindcă le evaluează diferit; interfața le arată ca una
 *  singură și le desparte abia la salvare. */
type Exception = {
  kind: "alert" | "ignore";
  store: string; category: string;
  // TEXT BRUT, nu listă. Dacă valoarea câmpului se recalculează din listă la fiecare tastă
  // (`value={skus.join(", ")}`), virgula pe care tocmai ai scris-o e ștearsă înainte s-o vezi —
  // câmpul devine imposibil de folosit pentru mai multe valori. Parsăm abia la salvare.
  skusText: string;
  threshold: number; recipientsText: string; excludeText: string;
};

type CatDraft = { name: string; stores: string[]; skusText: string };

function toast(msg: string, isError = false) {
  (window as unknown as { shopify?: { toast: { show: (m: string, o?: { isError: boolean }) => void } } })
    .shopify?.toast.show(msg, isError ? { isError: true } : undefined);
}

const csv = (v: string) => v.split(/[,;\n]/).map((x) => x.trim()).filter(Boolean);
const shortMail = (e: string) => e.split("@")[0];

const joinList = (v?: string[]) => (v || []).join(", ");

function fromCfg(cfg: InventoryGuard): Exception[] {
  return [
    ...(cfg.rules ?? []).map((r): Exception => ({
      kind: "alert", store: r.store || "", category: r.category || "", skusText: joinList(r.skus),
      threshold: r.threshold, recipientsText: joinList(r.recipients),
      excludeText: joinList(r.exclude_recipients),
    })),
    ...(cfg.exclusions ?? []).map((e): Exception => ({
      kind: "ignore", store: e.store || "", category: e.category || "", skusText: joinList(e.skus),
      threshold: 0, recipientsText: "", excludeText: "",
    })),
  ];
}

function toCfg(cfg: InventoryGuard, list: Exception[], cats: CatDraft[]): InventoryGuard {
  return {
    ...cfg,
    categories: cats.map((c) => ({ name: c.name, stores: c.stores, skus: csv(c.skusText) })),
    rules: list.filter((x) => x.kind === "alert").map((x) => ({
      store: x.store, category: x.category, skus: csv(x.skusText), threshold: x.threshold,
      recipients: csv(x.recipientsText), exclude_recipients: csv(x.excludeText),
    })),
    exclusions: list.filter((x) => x.kind === "ignore").map((x) => ({
      store: x.store, category: x.category, skus: csv(x.skusText),
    })),
  };
}

/** Rândul închis trebuie să spună TOT ce face regula, într-o propoziție. Altfel „ce reguli am pus?"
 *  se răspunde doar deschizând fiecare formular pe rând. */
function summary(x: Exception) {
  const target = x.store || x.category || t("Whole group");
  const skus = csv(x.skusText);
  const what = skus.length
    ? (skus.length <= 2 ? skus.join(", ").toUpperCase() : `${skus.length} ${t("products")}`)
    : t("all products");
  return { target, what, plus: csv(x.recipientsText), minus: csv(x.excludeText) };
}

export function InventoryGuardCard() {
  const [cfg, setCfg] = useState<InventoryGuard | null>(null);
  const [list, setList] = useState<Exception[]>([]);
  const [catList, setCatList] = useState<CatDraft[]>([]);
  const [rcptText, setRcptText] = useState("");
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
      .then((c) => {
        setCfg(c);
        setList(fromCfg(c));
        setCatList((c.categories ?? []).map((x) => ({
          name: x.name, stores: x.stores || [], skusText: joinList(x.skus) })));
        setRcptText(joinList(c.recipients));
      })
      .catch((e) => setLoadError((e as Error).message || "eroare"));
  }, []);
  useEffect(() => {
    getOverviewStores()
      .then((r) => setStores((r as unknown as { stores?: { name?: string; domain?: string }[] })
        .stores?.map((s) => s.name || s.domain || "").filter(Boolean) ?? []))
      .catch(() => setStores([]));
  }, []);

  const cats = catList;
  const setCats = (fn: (cs: CatDraft[]) => CatDraft[]) => setCatList(fn);
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
    try {
      await saveInventoryGuard(toCfg({ ...cfg, recipients: csv(rcptText) }, list, catList));
      toast(t("Stock guard settings saved."));
    }
    catch (e) { toast(`${t("Couldn't save")}: ${(e as Error).message}`, true); }
    finally { setSaving(false); }
  }, [cfg, list, catList, rcptText]);

  const runNow = useCallback(async () => {
    setRunning(true);
    try {
      const r = await runInventoryGuard();
      toast(r.baseline
        ? `${t("Baseline")}: ${r.baseline} ${t("products already below threshold, recorded without email.")}`
        : `${r.checked ?? 0} ${t("checked")} · ${r.low ?? 0} ${t("below threshold")} · ${r.new_alerts ?? 0} ${t("new alerts")}.`);
    } catch (e) { toast(`${t("Run failed")}: ${(e as Error).message}`, true); }
    finally { setRunning(false); }
  }, []);

  if (!cfg) {
    return (
      <Card><BlockStack gap="200">
        <Text as="h2" variant="headingMd">{t("Stock guard")}</Text>
        <Text as="p" tone={loadError ? "critical" : "subdued"}>
          {loadError ? `${t("Couldn't load settings")}: ${loadError}` : t("Loading…")}
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
            {cfg.enabled ? <Badge tone="success">{t("On")}</Badge> : <Badge>{t("Off")}</Badge>}
          </InlineStack>
          <InlineStack gap="200">
            <Button loading={running} onClick={() => void runNow()}>{t("Check now")}</Button>
            <Button variant="primary" loading={saving} onClick={() => void save()}>{t("Save")}</Button>
          </InlineStack>
        </InlineStack>

        {cfg.smtp_ready === false && (
          <Banner tone="warning"><p>{t("SMTP isn't configured on the server — alerts can't be sent.")}</p></Banner>
        )}

        {/* ── Implicit ─────────────────────────────────────────────────────────────── */}
        <Box background="bg-surface-secondary" borderRadius="200" padding="300">
          <BlockStack gap="300">
            <InlineStack align="space-between" blockAlign="center" gap="200" wrap={false}>
              <Text as="span" variant="bodyMd">
                <b>{t("Default")}:</b> {t("alert below")} <b>{cfg.threshold ?? 50}</b> {t("units")}
                {csv(rcptText).length
                  ? <> · {t("send to")} <b>{csv(rcptText).map(shortMail).join(", ")}</b></>
                  : <Text as="span" tone="critical"> · {t("no recipients")}</Text>}
              </Text>
              <Button variant="tertiary" onClick={() => setEditDefault((v) => !v)}>
                {editDefault ? t("Done") : t("Edit")}
              </Button>
            </InlineStack>
            {editDefault && (
              <InlineGrid columns={{ xs: 1, md: "140px 150px 1fr" }} gap="300">
                <Select label={t("Alerts")} value={cfg.enabled ? "1" : "0"}
                  options={[{ label: t("On"), value: "1" }, { label: t("Off"), value: "0" }]}
                  onChange={(v) => setCfg({ ...cfg, enabled: v === "1" })} />
                <TextField label={t("Alert below")} type="number" autoComplete="off" suffix={t("units")}
                  value={String(cfg.threshold ?? 50)}
                  onChange={(v) => setCfg({ ...cfg, threshold: Number(v) || 0 })} />
                <TextField label={t("Send to")} autoComplete="off" placeholder="name@arona.ro"
                  value={rcptText} onChange={setRcptText} />
                <Box>
                  <TextField label={t("Re-arm margin")} type="number" autoComplete="off" suffix="%"
                    value={String(cfg.hysteresis_pct ?? 20)}
                    onChange={(v) => setCfg({ ...cfg, hysteresis_pct: Number(v) || 0 })}
                    helpText={t("New alert only after stock climbs back above the threshold plus this margin.")} />
                </Box>
              </InlineGrid>
            )}
          </BlockStack>
        </Box>

        {/* ── Excepții ─────────────────────────────────────────────────────────────── */}
        <BlockStack gap="200">
          <InlineStack align="space-between" blockAlign="center">
            <Text as="h3" variant="headingSm">{`${t("Exceptions")} (${list.length})`}</Text>
            <InlineStack gap="200">
              <Button variant="tertiary" onClick={() => setShowGroups((v) => !v)}>
                {`${t("Groups")} (${cats.length})`}
              </Button>
              <Button onClick={() => { setList((xs) => [...xs, {
                kind: "alert", store: "", category: "", skusText: "",
                threshold: cfg.threshold ?? 50, recipientsText: "", excludeText: "",
              }]); setOpen(list.length); }}>{t("Add")}</Button>
            </InlineStack>
          </InlineStack>

          {list.length === 0 && (
            <Text as="p" tone="subdued" variant="bodySm">
              {t("No exceptions — every product uses the default rule.")}
            </Text>
          )}

          {list.map((x, i) => {
            const { target, what, plus, minus } = summary(x);
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
                        ? <Badge tone="warning">{t("no alerts")}</Badge>
                        : <Badge tone="attention">{`${t("below")} ${x.threshold} ${t("units")}`}</Badge>}
                      {/* „+" si „−" erau ambele gri, iar minusul se citea ca o liniuță de
                          despărțire — nu puteai spune cine e adăugat și cine scos. Cuvinte, nu
                          semne, plus culoare pe excludere. */}
                      {x.kind === "alert" && plus.length > 0 && (
                        <Text as="span" tone="subdued" variant="bodySm">
                          {t("and") + " " + plus.map(shortMail).join(", ")}
                        </Text>
                      )}
                      {x.kind === "alert" && minus.length > 0 && (
                        <Text as="span" tone="critical" variant="bodySm" fontWeight="medium">
                          {t("without") + " " + minus.map(shortMail).join(", ")}
                        </Text>
                      )}
                    </InlineStack>
                    <InlineStack gap="100">
                      <Button variant="tertiary" onClick={() => setOpen(isOpen ? null : i)}>
                        {isOpen ? t("Done") : t("Edit")}
                      </Button>
                      <Button variant="tertiary" tone="critical"
                        onClick={() => { setList((xs) => xs.filter((_, j) => j !== i)); setOpen(null); }}>
                        {t("Delete")}
                      </Button>
                    </InlineStack>
                  </InlineStack>

                  {isOpen && (
                    <BlockStack gap="300">
                      <Divider />
                      <InlineGrid columns={{ xs: 1, md: "1.3fr 1.4fr 170px 120px" }} gap="300">
                        <Select label={t("Applies to")} options={targetOptions} value={targetValue(x)}
                          helpText={x.store ? t("This store's stock.")
                            : x.category ? t("The sum of the stores in the group.") : t("Total stock across the group.")}
                          onChange={(v) => patch(i, applyTarget(v))} />
                        <TextField label={t("Products (SKU)")} autoComplete="off"
                          placeholder={t("all, if empty")} helpText={t("Paste as many as you like, comma-separated.")}
                          value={x.skusText} onChange={(v) => patch(i, { skusText: v })} />
                        <Select label={t("Action")} value={x.kind}
                          options={[{ label: t("Alert below…"), value: "alert" },
                                    { label: t("Never alert"), value: "ignore" }]}
                          onChange={(v) => patch(i, { kind: v as Exception["kind"] })} />
                        {x.kind === "alert"
                          ? <TextField label={t("Threshold")} type="number" autoComplete="off" suffix={t("units")}
                              value={String(x.threshold)}
                              onChange={(v) => patch(i, { threshold: Number(v) || 0 })} />
                          : <div />}
                      </InlineGrid>
                      {x.kind === "alert" && (
                        <InlineGrid columns={{ xs: 1, md: "1fr 1fr" }} gap="300">
                          <TextField label={t("Recipients")} autoComplete="off" placeholder="someone@arona.ro"
                            value={x.recipientsText}
                            onChange={(v) => patch(i, { recipientsText: v })} />
                          <TextField label={t("Excluded")} autoComplete="off" placeholder="someone@arona.ro"
                            helpText={t("Even if they are in the default list.")}
                            value={x.excludeText}
                            onChange={(v) => patch(i, { excludeText: v })} />
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
                <Text as="h3" variant="headingSm">{t("Store groups")}</Text>
                <Text as="p" tone="subdued" variant="bodySm">
                  {t("One exception for several stores at once. The group's stock is their sum.")}
                </Text>
              </BlockStack>
              <Button onClick={() => { setCats((cs) => [...cs, { name: "", stores: [], skusText: "" }]);
                setOpenCat(cats.length); }}>{t("Add group")}</Button>
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
                          {c.name || t("(unnamed)")}
                        </Text>
                        <Text as="span" tone="subdued" variant="bodySm">
                          {(c.stores || []).length
                            ? `${(c.stores || []).length} ${t("stores")}`
                            : t("no stores")}
                          {csv(c.skusText).length ? ` · ${csv(c.skusText).length} ${t("products")}` : ""}
                        </Text>
                      </InlineStack>
                      <InlineStack gap="100">
                        <Button variant="tertiary" onClick={() => setOpenCat(isOpen ? null : i)}>
                          {isOpen ? t("Done") : t("Edit")}
                        </Button>
                        <Button variant="tertiary" tone="critical"
                          onClick={() => { setCats((cs) => cs.filter((_, j) => j !== i)); setOpenCat(null); }}>
                          {t("Delete")}
                        </Button>
                      </InlineStack>
                    </InlineStack>

                    {isOpen && (
                      <BlockStack gap="300">
                        <Divider />
                        <InlineGrid columns={{ xs: 1, md: "240px 1fr" }} gap="300">
                          <BlockStack gap="300">
                            <TextField label={t("Name")} autoComplete="off" placeholder="perfumes"
                              value={c.name}
                              onChange={(v) => setCats((cs) => cs.map((x, j) => (j === i ? { ...x, name: v } : x)))} />
                            <TextField label={t("Only these products (SKU)")} autoComplete="off"
                              placeholder={t("all, if empty")} value={c.skusText}
                              onChange={(v) => setCats((cs) => cs.map((x, j) => (j === i ? { ...x, skusText: v } : x)))} />
                          </BlockStack>
                          {/* Bifat, nu scris: numele au apostrofuri și diacritice („Maison d'Esteban"),
                              iar o literă greșită face grupul să nu prindă nimic — tăcut. */}
                          <div style={{ maxHeight: 200, overflowY: "auto",
                            border: "1px solid var(--p-color-border)", borderRadius: 8, padding: 12 }}>
                            <ChoiceList allowMultiple title={t("Stores in group")}
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

import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { COACH_KEY } from "../components/TestModeCoach";
import { useLang } from "../lib/i18n";
import {
  Badge,
  BlockStack,
  Banner,
  Button,
  ButtonGroup,
  Card,
  Checkbox,
  Divider,
  InlineStack,
  Page,
  ResourceItem,
  ResourceList,
  Select,
  SkeletonBodyText,
  Tabs,
  Text,
  TextField,
  Thumbnail,
} from "@shopify/polaris";
import {
  ApiError,
  getTestMode,
  setTestMode,
  createBox,
  createProfile,
  createRule,
  deleteBox,
  deletePackingRule,
  deleteProfile,
  deleteRule,
  getAutomationSettings,
  getBoxes,
  getCouriers,
  getLinkCode,
  getOrg,
  getPackingRules,
  getProducts,
  joinOrg,
  leaveOrg,
  listRules,
  setStoreGroup,
  syncStatuses,
  updateAutomationSettings,
  saveProductPacking,
  setProductBarcodes,
  productBarcodeLabels,
  updateBox,
  updateProfile,
  updateRule,
  upsertPackingRules,
  type AutomationSettings,
  type CatalogProduct,
  type CourierAccount,
  type CouriersResponse,
  type OrgResponse,
  type PackingBox,
  type PackingBoxInput,
  type PackingRule,
  type ProductPackingItem,
  type ShipmentProfile,
  type ShipmentProfileInput,
  type ShipmentRule,
  type ShipmentRuleInput,
} from "../lib/api";
import { InvoiceSettingsCard } from "../components/InvoiceSettingsCard";
import { AutomationScheduleCard } from "../components/AutomationScheduleCard";

const HOURS = Array.from({ length: 24 }, (_, h) => ({ label: `${String(h).padStart(2, "0")}:00`, value: String(h) }));
const DELAY_OPTIONS = [
  { label: "As soon as eligible", value: "0" },
  { label: "After 30 minutes", value: "30" },
  { label: "After 1 hour", value: "60" },
  { label: "After 2 hours", value: "120" },
  { label: "After 4 hours", value: "240" },
  { label: "After 24 hours", value: "1440" },
];

function toast(msg: string, isError = false) {
  (window as any).shopify?.toast?.show(msg, isError ? { isError: true } : undefined);
}

function AutomationCard() {
  const [s, setS] = useState<AutomationSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [awbAccounts, setAwbAccounts] = useState<CourierAccount[]>([]);
  const [awbProfiles, setAwbProfiles] = useState<ShipmentProfile[]>([]);

  useEffect(() => {
    getAutomationSettings()
      .then(setS)
      .catch((e) => setError(e instanceof ApiError ? e.message : "Failed to load automation settings."))
      .finally(() => setLoading(false));
    getCouriers()
      .then((r) => {
        setAwbAccounts(r.accounts.filter((a) => a.has_credentials && a.is_active));
        setAwbProfiles(r.profiles);
      })
      .catch(() => setAwbAccounts([]));
  }, []);

  const patch = (p: Partial<AutomationSettings>) => setS((prev) => (prev ? { ...prev, ...p } : prev));

  const set = (k: keyof AutomationSettings, v: boolean) => setS((prev) => (prev ? { ...prev, [k]: v } : prev));

  const save = async () => {
    if (!s) return;
    setSaving(true);
    try {
      setS(await updateAutomationSettings(s));
      toast("Automation settings saved");
    } catch (e) {
      toast(e instanceof Error ? e.message : "Save failed", true);
    } finally {
      setSaving(false);
    }
  };

  const runSync = async () => {
    setSyncing(true);
    try {
      const r = await syncStatuses();
      toast(`Synced ${r.processed} shipment(s)`);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Sync failed", true);
    } finally {
      setSyncing(false);
    }
  };

  return (
    <Card>
      <BlockStack gap="400">
        <BlockStack gap="100">
          <Text as="h2" variant="headingMd">Delivery & refusal automation</Text>
          <Text as="p" tone="subdued">
            Keep Shopify in step with the courier — mark orders shipped with tracking, push live
            delivery status, and handle refused COD parcels automatically.
          </Text>
        </BlockStack>

        {error && (
          <Banner tone="critical" onDismiss={() => setError(null)}>
            <p>{error}</p>
          </Banner>
        )}

        {loading || !s ? (
          <SkeletonBodyText lines={5} />
        ) : (
          <BlockStack gap="500">
            <TextField
              label="Sender name on the AWB"
              value={s.sender_name ?? ""}
              onChange={(v) => setS((prev) => (prev ? { ...prev, sender_name: v } : prev))}
              autoComplete="off"
              placeholder="e.g. Your store name"
              helpText="Printed as the sender/expeditor on labels (DPD, GLS). Leave empty to use the courier account's default. Useful when you ship several stores from one account."
            />

            <TextField
              label="AWB content / shipping description"
              value={s.content_template ?? ""}
              onChange={(v) => setS((prev) => (prev ? { ...prev, content_template: v } : prev))}
              autoComplete="off"
              placeholder="${orderName} / ${quantity} x ${sku}"
              helpText="Printed as the parcel contents. Placeholders: ${orderName}, ${quantity}, ${sku}. Leave empty for the default, or write your own."
            />

            <Divider />

            <BlockStack gap="200">
              <Checkbox
                label="Sync courier status to Shopify"
                helpText="Poll each AWB and, once the parcel is moving, mark the order fulfilled with tracking and push live delivery events (in transit → delivered)."
                checked={s.status_sync_enabled}
                onChange={(v) => set("status_sync_enabled", v)}
              />
              <div style={{ paddingInlineStart: "28px" }}>
                <Checkbox
                  label="Email the customer the shipping notification"
                  helpText="When Shopify creates the fulfillment, send the buyer the tracking email. Off = fulfill silently."
                  checked={s.fulfill_notify_customer}
                  disabled={!s.status_sync_enabled}
                  onChange={(v) => set("fulfill_notify_customer", v)}
                />
              </div>
            </BlockStack>

            <Divider />

            <BlockStack gap="200">
              <Checkbox
                label="Auto-cancel refused / returned COD orders"
                helpText="When the courier reports the parcel refused or returned, cancel the order automatically."
                checked={s.auto_cancel_on_refusal}
                onChange={(v) => set("auto_cancel_on_refusal", v)}
              />
              <div style={{ paddingInlineStart: "28px" }}>
                <BlockStack gap="200">
                  <Checkbox
                    label="Restock the items on cancel"
                    checked={s.refusal_restock}
                    disabled={!s.auto_cancel_on_refusal}
                    onChange={(v) => set("refusal_restock", v)}
                  />
                  <Checkbox
                    label="Email the customer when the order is cancelled"
                    checked={s.refusal_notify_customer}
                    disabled={!s.auto_cancel_on_refusal}
                    onChange={(v) => set("refusal_notify_customer", v)}
                  />
                </BlockStack>
              </div>
              <Banner tone="info">
                <p>
                  Paid orders (card or any prepaid method) are <b>never</b> auto-cancelled — a refund
                  is your call, so the app leaves those for you to handle.
                </p>
              </Banner>
            </BlockStack>

            <Divider />

            <BlockStack gap="300">
              <Checkbox
                label="Create AWBs automatically"
                helpText="On a schedule, make AWBs for orders with a valid address that don't have one yet. Nothing runs until you pick a courier below."
                checked={s.auto_awb_enabled}
                onChange={(v) => patch({ auto_awb_enabled: v })}
              />
              <div style={{ paddingInlineStart: "28px" }}>
                <BlockStack gap="300">
                  <Select
                    label="Shipment profile for automation"
                    options={[{ label: "— none (use courier below) —", value: "" },
                      ...awbProfiles.map((p) => ({ label: p.name, value: String(p.id) }))]}
                    value={s.auto_awb_profile_id != null ? String(s.auto_awb_profile_id) : ""}
                    onChange={(v) => patch({ auto_awb_profile_id: v ? Number(v) : null })}
                    disabled={!s.auto_awb_enabled}
                    helpText="Preferred — automatic labels use this preset (courier + parcels + weight + content). Overrides the courier below."
                  />
                  <Select
                    label="Courier for automation"
                    options={[{ label: "— select a courier —", value: "" },
                      ...awbAccounts.map((a) => ({ label: `${a.name} (${a.account_key})`, value: a.account_key }))]}
                    value={s.auto_awb_account_key ?? ""}
                    onChange={(v) => patch({ auto_awb_account_key: v || null })}
                    disabled={!s.auto_awb_enabled || s.auto_awb_profile_id != null}
                    helpText={s.auto_awb_profile_id != null ? "A profile is selected above; it takes over." : undefined}
                  />
                  <Select
                    label="Make the AWB…"
                    options={DELAY_OPTIONS}
                    value={String(s.auto_awb_delay_minutes ?? 0)}
                    onChange={(v) => patch({ auto_awb_delay_minutes: Number(v) })}
                    disabled={!s.auto_awb_enabled}
                    helpText="How long after the order is placed — a buffer for cancellations / address fixes."
                  />
                  <Select
                    label="Run"
                    options={[{ label: "All day (continuous)", value: "all" }, { label: "Only within an interval", value: "interval" }]}
                    value={s.awb_window_start == null || s.awb_window_end == null ? "all" : "interval"}
                    onChange={(v) => patch(v === "all"
                      ? { awb_window_start: null, awb_window_end: null }
                      : { awb_window_start: s.awb_window_start ?? 9, awb_window_end: s.awb_window_end ?? 18 })}
                    disabled={!s.auto_awb_enabled}
                  />
                  {s.awb_window_start != null && s.awb_window_end != null && (
                    <InlineStack gap="300">
                      <Select label="From" options={HOURS} value={String(s.awb_window_start)}
                        onChange={(v) => patch({ awb_window_start: Number(v) })} disabled={!s.auto_awb_enabled} />
                      <Select label="To" options={HOURS} value={String(s.awb_window_end)}
                        onChange={(v) => patch({ awb_window_end: Number(v) })} disabled={!s.auto_awb_enabled} />
                    </InlineStack>
                  )}
                </BlockStack>
              </div>
              <Banner tone="info">
                <p>Only orders with a <b>valid address</b> are auto-dispatched, and never twice. Times are Romania (Europe/Bucharest).</p>
              </Banner>
            </BlockStack>

            <InlineStack gap="300">
              <Button variant="primary" loading={saving} onClick={save}>Save</Button>
              <Button loading={syncing} onClick={runSync}>Sync statuses now</Button>
            </InlineStack>
          </BlockStack>
        )}
      </BlockStack>
    </Card>
  );
}

function OrganizationCard() {
  const [org, setOrg] = useState<OrgResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [code, setCode] = useState("");
  const [linkCode, setLinkCode] = useState<string | null>(null);
  const [group, setGroup] = useState("");
  const [busy, setBusy] = useState(false);

  const load = async () => {
    try {
      const r = await getOrg();
      setOrg(r);
      setGroup(r.me.store_group ?? "");
    } catch {
      /* ignore */
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { load(); }, []);

  const run = async (fn: () => Promise<unknown>, ok?: string) => {
    setBusy(true);
    try { await fn(); if (ok) toast(ok); await load(); }
    catch (e) { toast(e instanceof Error ? e.message : "Failed", true); }
    finally { setBusy(false); }
  };

  const showCode = () => run(async () => { setLinkCode((await getLinkCode()).link_code); });
  const doJoin = () => run(async () => { const r = await joinOrg(code.trim()); setCode(""); toast(r.message); });
  const doLeave = () => run(() => leaveOrg(), "Unlinked this store");
  const doGroup = () => run(() => setStoreGroup(group.trim()), "Group saved");

  return (
    <Card>
      <BlockStack gap="400">
        <BlockStack gap="100">
          <Text as="h2" variant="headingMd">Organization (multi-store)</Text>
          <Text as="p" tone="subdued">
            Link several of your stores to run them from one dashboard — then Orders can show one
            store, a group, or all of them. Install Order Hub on each store and share the link code.
          </Text>
        </BlockStack>

        {loading || !org ? (
          <SkeletonBodyText lines={4} />
        ) : org.organization ? (
          <BlockStack gap="400">
            <InlineStack gap="200" blockAlign="center">
              <Badge tone="success">Linked</Badge>
              <Text as="span">{org.stores.length} store(s) in “{org.organization.name ?? "organization"}”</Text>
            </InlineStack>
            <ResourceList
              resourceName={{ singular: "store", plural: "stores" }}
              items={org.stores}
              renderItem={(st) => (
                <ResourceItem id={String(st.id)} onClick={() => {}}>
                  <InlineStack align="space-between" blockAlign="center">
                    <BlockStack gap="050">
                      <InlineStack gap="200" blockAlign="center">
                        <Text as="span" fontWeight="semibold">{st.name ?? st.domain}</Text>
                        {st.is_me && <Badge>This store</Badge>}
                      </InlineStack>
                      <Text as="span" tone="subdued">
                        {st.domain}{st.store_group ? ` · group: ${st.store_group}` : ""}
                        {st.sender_name ? ` · sender: ${st.sender_name}` : ""}
                      </Text>
                    </BlockStack>
                  </InlineStack>
                </ResourceItem>
              )}
            />
            <InlineStack gap="200" blockAlign="end">
              <div style={{ flex: 1 }}>
                <TextField label="This store's group" value={group} onChange={setGroup} autoComplete="off"
                  placeholder="e.g. Deals brands" helpText="Group stores to filter the portfolio." />
              </div>
              <Button onClick={doGroup} loading={busy}>Save group</Button>
            </InlineStack>
            <Divider />
            <InlineStack gap="300" align="space-between" blockAlign="center">
              <Button onClick={showCode} loading={busy}>Show link code</Button>
              <Button tone="critical" variant="tertiary" onClick={doLeave} loading={busy}>Unlink this store</Button>
            </InlineStack>
            {linkCode && (
              <Banner tone="info">
                <p>Share this code in another store's Order Hub → Settings → “Link this store”: <b>{linkCode}</b></p>
              </Banner>
            )}
          </BlockStack>
        ) : (
          <BlockStack gap="400">
            <Banner tone="warning"><p>This store isn't linked to any organization yet.</p></Banner>
            <InlineStack gap="200" blockAlign="end">
              <div style={{ flex: 1 }}>
                <TextField label="Link code" value={code} onChange={setCode} autoComplete="off"
                  placeholder="Paste a code from another of your stores" />
              </div>
              <Button variant="primary" onClick={doJoin} loading={busy} disabled={!code.trim()}>Link this store</Button>
            </InlineStack>
            <Button onClick={showCode} loading={busy}>Create organization & show my link code</Button>
            {linkCode && (
              <Banner tone="info"><p>Your link code (paste it in your other stores): <b>{linkCode}</b></p></Banner>
            )}
          </BlockStack>
        )}
      </BlockStack>
    </Card>
  );
}

const PAYER_OPTIONS = [
  { label: "Sender pays courier", value: "SENDER" },
  { label: "Recipient pays courier", value: "RECIPIENT" },
];
const PACKING_OPTIONS = [
  { label: "Default / Box", value: "" },
  { label: "Box", value: "BOX" },
  { label: "Pallet", value: "PALLET" },
  { label: "Envelope", value: "ENVELOPE" },
  { label: "Bag", value: "BAG" },
  { label: "Wrap", value: "WRAP" },
];
const LABEL_SIZE_OPTIONS = [
  { label: "A6 (thermal)", value: "A6" },
  { label: "A4", value: "A4" },
];

type ProfileForm = {
  name: string;
  account_key: string;
  default_parcels: string;
  default_weight_kg: string;
  default_length_cm: string;
  default_width_cm: string;
  default_height_cm: string;
  default_service_id: string;
  default_payer: string;
  default_packing: string;
  default_label_size: string;
  content_template: string;
};

function blankForm(account_key = ""): ProfileForm {
  return {
    name: "",
    account_key,
    default_parcels: "1",
    default_weight_kg: "1",
    default_length_cm: "",
    default_width_cm: "",
    default_height_cm: "",
    default_service_id: "",
    default_payer: "SENDER",
    default_packing: "",
    default_label_size: "A6",
    content_template: "${orderName} / ${quantity} x ${sku}",
  };
}

function formFromProfile(p: ShipmentProfile): ProfileForm {
  const s = (v: number | null) => (v == null ? "" : String(v));
  return {
    name: p.name,
    account_key: p.account_key,
    default_parcels: s(p.default_parcels) || "1",
    default_weight_kg: s(p.default_weight_kg) || "1",
    default_length_cm: s(p.default_length_cm),
    default_width_cm: s(p.default_width_cm),
    default_height_cm: s(p.default_height_cm),
    default_service_id: s(p.default_service_id),
    default_payer: p.default_payer ?? "SENDER",
    default_packing: p.default_packing ?? "",
    default_label_size: p.default_label_size ?? "A6",
    content_template: p.content_template ?? "",
  };
}

function toProfileInput(f: ProfileForm): ShipmentProfileInput {
  const num = (v: string) => (v.trim() === "" ? null : Number(v));
  return {
    name: f.name.trim(),
    account_key: f.account_key,
    default_parcels: num(f.default_parcels) ?? 1,
    default_weight_kg: num(f.default_weight_kg) ?? 1,
    default_length_cm: num(f.default_length_cm),
    default_width_cm: num(f.default_width_cm),
    default_height_cm: num(f.default_height_cm),
    default_service_id: num(f.default_service_id),
    default_payer: f.default_payer || null,
    default_packing: f.default_packing || null,
    default_label_size: f.default_label_size || null,
    content_template: f.content_template.trim() || null,
  };
}

// Full manager: list + add/edit/delete. A profile bundles courier + parcels + weight + dims +
// content so the operator picks it ONCE and creates AWBs in one click — the "don't re-enter the
// same thing every order" ask. Self-contained (fetches + refreshes its own list).
function ShipmentProfilesCard() {
  const [profiles, setProfiles] = useState<ShipmentProfile[]>([]);
  const [accounts, setAccounts] = useState<CourierAccount[]>([]);
  const [loading, setLoading] = useState(true);
  const [editId, setEditId] = useState<number | null | undefined>(undefined); // undefined=closed, null=new
  const [form, setForm] = useState<ProfileForm>(blankForm());
  const [saving, setSaving] = useState(false);

  const refresh = async () => {
    const r = await getCouriers();
    setProfiles(r.profiles);
    setAccounts(r.accounts.filter((a) => a.has_credentials && a.is_active));
  };
  useEffect(() => {
    refresh().catch(() => {}).finally(() => setLoading(false));
  }, []);

  const accountOptions = accounts.map((a) => ({ label: `${a.name} (${a.account_key})`, value: a.account_key }));
  const set = (k: keyof ProfileForm) => (v: string) => setForm((f) => ({ ...f, [k]: v }));

  const openNew = () => { setForm(blankForm(accounts[0]?.account_key ?? "")); setEditId(null); };
  const openEdit = (p: ShipmentProfile) => { setForm(formFromProfile(p)); setEditId(p.id); };
  const close = () => setEditId(undefined);

  const save = async () => {
    if (!form.name.trim() || !form.account_key) { toast("Name and courier are required", true); return; }
    setSaving(true);
    try {
      const input = toProfileInput(form);
      if (editId == null) await createProfile(input);
      else await updateProfile(editId, input);
      await refresh();
      close();
      toast("Profile saved");
    } catch (e) {
      toast(e instanceof Error ? e.message : "Save failed", true);
    } finally {
      setSaving(false);
    }
  };

  const remove = async (id: number) => {
    try {
      await deleteProfile(id);
      await refresh();
      toast("Profile deleted");
    } catch (e) {
      toast(e instanceof Error ? e.message : "Delete failed", true);
    }
  };

  return (
    <Card>
      <BlockStack gap="300">
        <InlineStack align="space-between" blockAlign="center">
          <Text as="h2" variant="headingMd">Shipment profiles</Text>
          {editId === undefined && accounts.length > 0 && (
            <Button variant="primary" onClick={openNew}>Add profile</Button>
          )}
        </InlineStack>
        <Text as="p" tone="subdued">
          Save a courier + parcel + content preset once, then create AWBs in one click — no re-picking every order.
        </Text>

        {loading ? (
          <SkeletonBodyText lines={3} />
        ) : accounts.length === 0 ? (
          <Banner tone="warning" title="Add a courier account first">
            <p>A profile needs a courier account with credentials. Add one above, then come back.</p>
          </Banner>
        ) : editId === undefined && profiles.length === 0 ? (
          <Text as="p" tone="subdued">No shipment profiles yet. Add one to enable one-click AWB.</Text>
        ) : editId === undefined ? (
          <ResourceList
            resourceName={{ singular: "profile", plural: "profiles" }}
            items={profiles}
            renderItem={(p) => (
              <ResourceItem id={String(p.id)} onClick={() => openEdit(p)}>
                <InlineStack align="space-between" blockAlign="center">
                  <BlockStack gap="050">
                    <Text as="span" fontWeight="semibold">{p.name}</Text>
                    <Text as="span" tone="subdued">
                      {p.account_key} · {p.default_parcels ?? 1} parcel(s) · {p.default_weight_kg ?? 1} kg
                      {p.default_service_id ? ` · svc ${p.default_service_id}` : ""}
                    </Text>
                  </BlockStack>
                  <InlineStack gap="200">
                    <Button onClick={() => openEdit(p)}>Edit</Button>
                    <Button tone="critical" variant="plain" onClick={() => remove(p.id)}>Delete</Button>
                  </InlineStack>
                </InlineStack>
              </ResourceItem>
            )}
          />
        ) : null}

        {editId !== undefined && (
          <BlockStack gap="300">
            <Divider />
            <Text as="h3" variant="headingSm">{editId == null ? "New profile" : "Edit profile"}</Text>
            <TextField label="Profile name" value={form.name} onChange={set("name")} autoComplete="off" placeholder="e.g. DPD home · 1 kg" />
            <Select label="Courier account" options={accountOptions} value={form.account_key} onChange={set("account_key")} />
            <InlineStack gap="300">
              <div style={{ flex: 1 }}><TextField label="Parcels" type="number" value={form.default_parcels} onChange={set("default_parcels")} autoComplete="off" /></div>
              <div style={{ flex: 1 }}><TextField label="Weight (kg)" type="number" value={form.default_weight_kg} onChange={set("default_weight_kg")} autoComplete="off" /></div>
              <div style={{ flex: 1 }}><TextField label="Service ID (optional)" type="number" value={form.default_service_id} onChange={set("default_service_id")} autoComplete="off" /></div>
            </InlineStack>
            <InlineStack gap="300">
              <div style={{ flex: 1 }}><TextField label="Length (cm)" type="number" value={form.default_length_cm} onChange={set("default_length_cm")} autoComplete="off" /></div>
              <div style={{ flex: 1 }}><TextField label="Width (cm)" type="number" value={form.default_width_cm} onChange={set("default_width_cm")} autoComplete="off" /></div>
              <div style={{ flex: 1 }}><TextField label="Height (cm)" type="number" value={form.default_height_cm} onChange={set("default_height_cm")} autoComplete="off" /></div>
            </InlineStack>
            <InlineStack gap="300">
              <div style={{ flex: 1 }}><Select label="Courier paid by" options={PAYER_OPTIONS} value={form.default_payer} onChange={set("default_payer")} /></div>
              <div style={{ flex: 1 }}><Select label="Packing" options={PACKING_OPTIONS} value={form.default_packing} onChange={set("default_packing")} /></div>
              <div style={{ flex: 1 }}><Select label="Label size" options={LABEL_SIZE_OPTIONS} value={form.default_label_size} onChange={set("default_label_size")} /></div>
            </InlineStack>
            <TextField
              label="AWB content template"
              value={form.content_template}
              onChange={set("content_template")}
              autoComplete="off"
              helpText={"Variables: ${orderName}, ${quantity}, ${sku}, ${productName}, ${orderNote}"}
            />
            <InlineStack gap="200">
              <Button variant="primary" onClick={save} loading={saving}>Save profile</Button>
              <Button onClick={close}>Cancel</Button>
            </InlineStack>
          </BlockStack>
        )}
      </BlockStack>
    </Card>
  );
}

type RuleForm = {
  name: string;
  priority: string;
  enabled: boolean;
  profile_id: string;
  tags: string;
  skus: string;
  content: string;
  county: string;
  city: string;
  country: string;
  total_min: string;
  total_max: string;
  items_min: string;
  items_max: string;
};

function blankRule(profile_id = ""): RuleForm {
  return {
    name: "", priority: "100", enabled: true, profile_id,
    tags: "", skus: "", content: "", county: "", city: "", country: "",
    total_min: "", total_max: "", items_min: "", items_max: "",
  };
}

function ruleToForm(r: ShipmentRule): RuleForm {
  const c = r.conditions ?? {};
  const csv = (a?: string[]) => (a ?? []).join(", ");
  const s = (v?: number) => (v == null ? "" : String(v));
  return {
    name: r.name,
    priority: String(r.priority ?? 100),
    enabled: r.enabled,
    profile_id: String(r.profile_id),
    tags: csv(c.tags_any),
    skus: csv(c.sku_any),
    content: c.content_contains ?? "",
    county: csv(c.county_any),
    city: c.city_contains ?? "",
    country: csv(c.country_any),
    total_min: s(c.total_min),
    total_max: s(c.total_max),
    items_min: s(c.items_min),
    items_max: s(c.items_max),
  };
}

function toRuleInput(f: RuleForm): ShipmentRuleInput {
  const list = (v: string) => v.split(",").map((x) => x.trim()).filter(Boolean);
  const num = (v: string) => (v.trim() === "" ? undefined : Number(v));
  const conditions: ShipmentRule["conditions"] = {};
  if (list(f.tags).length) conditions.tags_any = list(f.tags);
  if (list(f.skus).length) conditions.sku_any = list(f.skus);
  if (f.content.trim()) conditions.content_contains = f.content.trim();
  if (list(f.county).length) conditions.county_any = list(f.county);
  if (f.city.trim()) conditions.city_contains = f.city.trim();
  if (list(f.country).length) conditions.country_any = list(f.country);
  if (num(f.total_min) !== undefined) conditions.total_min = num(f.total_min);
  if (num(f.total_max) !== undefined) conditions.total_max = num(f.total_max);
  if (num(f.items_min) !== undefined) conditions.items_min = num(f.items_min);
  if (num(f.items_max) !== undefined) conditions.items_max = num(f.items_max);
  return {
    name: f.name.trim(),
    priority: num(f.priority) ?? 100,
    enabled: f.enabled,
    conditions,
    profile_id: Number(f.profile_id),
  };
}

function ruleSummary(c: ShipmentRule["conditions"]): string {
  const parts: string[] = [];
  if (c.tags_any?.length) parts.push(`tags: ${c.tags_any.join("/")}`);
  if (c.sku_any?.length) parts.push(`SKU: ${c.sku_any.join("/")}`);
  if (c.content_contains) parts.push(`content ~ "${c.content_contains}"`);
  if (c.county_any?.length) parts.push(`county: ${c.county_any.join("/")}`);
  if (c.city_contains) parts.push(`city ~ "${c.city_contains}"`);
  if (c.country_any?.length) parts.push(`country: ${c.country_any.join("/")}`);
  if (c.total_min != null) parts.push(`≥ ${c.total_min} RON`);
  if (c.total_max != null) parts.push(`≤ ${c.total_max} RON`);
  if (c.items_min != null) parts.push(`≥ ${c.items_min} pcs`);
  if (c.items_max != null) parts.push(`≤ ${c.items_max} pcs`);
  return parts.length ? parts.join(" · ") : "any order (catch-all)";
}

// Conditional routing for automation: IF the order matches (tags / SKU / content / county / city /
// country / total) THEN ship it with the chosen profile (courier + parcels + weight + content).
// Rules run top-down by priority; first match wins. Self-contained (fetches + refreshes its own list).
function ShipmentRulesCard() {
  const [rules, setRules] = useState<ShipmentRule[]>([]);
  const [profiles, setProfiles] = useState<ShipmentProfile[]>([]);
  const [loading, setLoading] = useState(true);
  const [editId, setEditId] = useState<number | null | undefined>(undefined); // undefined=closed, null=new
  const [form, setForm] = useState<RuleForm>(blankRule());
  const [saving, setSaving] = useState(false);

  const refresh = async () => {
    const [c, rr] = await Promise.all([getCouriers(), listRules()]);
    setProfiles(c.profiles);
    setRules(rr.rules);
  };
  useEffect(() => {
    refresh().catch(() => {}).finally(() => setLoading(false));
  }, []);

  const profileName = (id: number) => profiles.find((p) => p.id === id)?.name ?? `#${id}`;
  const profileOptions = profiles.map((p) => ({ label: p.name, value: String(p.id) }));
  const set = (k: keyof RuleForm) => (v: string) => setForm((f) => ({ ...f, [k]: v }));

  const openNew = () => { setForm(blankRule(profiles[0] ? String(profiles[0].id) : "")); setEditId(null); };
  const openEdit = (r: ShipmentRule) => { setForm(ruleToForm(r)); setEditId(r.id); };
  const close = () => setEditId(undefined);

  const save = async () => {
    if (!form.name.trim() || !form.profile_id) { toast("Name and profile are required", true); return; }
    setSaving(true);
    try {
      const input = toRuleInput(form);
      if (editId == null) await createRule(input);
      else await updateRule(editId, input);
      await refresh();
      close();
      toast("Rule saved");
    } catch (e) {
      toast(e instanceof Error ? e.message : "Save failed", true);
    } finally {
      setSaving(false);
    }
  };

  const remove = async (id: number) => {
    try { await deleteRule(id); await refresh(); toast("Rule deleted"); }
    catch (e) { toast(e instanceof Error ? e.message : "Delete failed", true); }
  };

  return (
    <Card>
      <BlockStack gap="300">
        <InlineStack align="space-between" blockAlign="center">
          <Text as="h2" variant="headingMd">Automation routing rules</Text>
          {editId === undefined && profiles.length > 0 && (
            <Button variant="primary" onClick={openNew}>Add rule</Button>
          )}
        </InlineStack>
        <Text as="p" tone="subdued">
          When automatic shipping runs, orders are routed by these rules (top-down, first match wins) — e.g.
          a tag, SKU, county or order total picks a specific profile (courier + parcels). No match → the default profile above.
        </Text>

        {loading ? (
          <SkeletonBodyText lines={3} />
        ) : profiles.length === 0 ? (
          <Banner tone="warning" title="Create a shipment profile first">
            <p>A rule points at a profile (which carries the courier + parcels). Add a profile above, then come back.</p>
          </Banner>
        ) : editId === undefined && rules.length === 0 ? (
          <Text as="p" tone="subdued">No rules yet. Add one to route orders by tag, content, county, etc.</Text>
        ) : editId === undefined ? (
          <ResourceList
            resourceName={{ singular: "rule", plural: "rules" }}
            items={rules}
            renderItem={(r) => (
              <ResourceItem id={String(r.id)} onClick={() => openEdit(r)}>
                <InlineStack align="space-between" blockAlign="center">
                  <BlockStack gap="050">
                    <InlineStack gap="200" blockAlign="center">
                      <Badge tone={r.enabled ? "success" : undefined}>{r.enabled ? `#${r.priority}` : "off"}</Badge>
                      <Text as="span" fontWeight="semibold">{r.name}</Text>
                    </InlineStack>
                    <Text as="span" tone="subdued">{ruleSummary(r.conditions)} → {profileName(r.profile_id)}</Text>
                  </BlockStack>
                  <InlineStack gap="200">
                    <Button onClick={() => openEdit(r)}>Edit</Button>
                    <Button tone="critical" variant="plain" onClick={() => remove(r.id)}>Delete</Button>
                  </InlineStack>
                </InlineStack>
              </ResourceItem>
            )}
          />
        ) : null}

        {editId !== undefined && (
          <BlockStack gap="300">
            <Divider />
            <Text as="h3" variant="headingSm">{editId == null ? "New rule" : "Edit rule"}</Text>
            <InlineStack gap="300">
              <div style={{ flex: 2 }}><TextField label="Rule name" value={form.name} onChange={set("name")} autoComplete="off" placeholder="e.g. Fragile → DPD 2 parcels" /></div>
              <div style={{ flex: 1 }}><TextField label="Priority" type="number" value={form.priority} onChange={set("priority")} autoComplete="off" helpText="Lower runs first" /></div>
            </InlineStack>
            <Select label="Ship with profile" options={profileOptions} value={form.profile_id} onChange={set("profile_id")} />
            <Checkbox label="Enabled" checked={form.enabled} onChange={(v) => setForm((f) => ({ ...f, enabled: v }))} />
            <Text as="p" tone="subdued" variant="bodySm">Conditions — leave blank to ignore. All filled conditions must match (comma-separated = any of).</Text>
            <InlineStack gap="300">
              <div style={{ flex: 1 }}><TextField label="Order tags" value={form.tags} onChange={set("tags")} autoComplete="off" placeholder="fragil, urgent" /></div>
              <div style={{ flex: 1 }}><TextField label="SKUs" value={form.skus} onChange={set("skus")} autoComplete="off" placeholder="HA-0002, HA-0088" /></div>
            </InlineStack>
            <TextField label="Content contains" value={form.content} onChange={set("content")} autoComplete="off" placeholder="parfum" helpText="Matches a product title or SKU." />
            <InlineStack gap="300">
              <div style={{ flex: 1 }}><TextField label="County (județ)" value={form.county} onChange={set("county")} autoComplete="off" placeholder="Cluj, Bihor" /></div>
              <div style={{ flex: 1 }}><TextField label="City contains" value={form.city} onChange={set("city")} autoComplete="off" placeholder="Cluj-Napoca" /></div>
              <div style={{ flex: 1 }}><TextField label="Country" value={form.country} onChange={set("country")} autoComplete="off" placeholder="RO" /></div>
            </InlineStack>
            <InlineStack gap="300">
              <div style={{ flex: 1 }}><TextField label="Order total ≥" type="number" value={form.total_min} onChange={set("total_min")} autoComplete="off" /></div>
              <div style={{ flex: 1 }}><TextField label="Order total ≤" type="number" value={form.total_max} onChange={set("total_max")} autoComplete="off" /></div>
              <div style={{ flex: 1 }}><TextField label="Units (pcs) ≥" type="number" value={form.items_min} onChange={set("items_min")} autoComplete="off" /></div>
              <div style={{ flex: 1 }}><TextField label="Units (pcs) ≤" type="number" value={form.items_max} onChange={set("items_max")} autoComplete="off" /></div>
            </InlineStack>
            <InlineStack gap="200">
              <Button variant="primary" onClick={save} loading={saving}>Save rule</Button>
              <Button onClick={close}>Cancel</Button>
            </InlineStack>
          </BlockStack>
        )}
      </BlockStack>
    </Card>
  );
}

const ROUNDING_OPTIONS = [
  { label: "Share parcels across the order (fewest parcels)", value: "shared" },
  { label: "Separate parcels per product", value: "per_product" },
];
const BOX_TYPE_OPTIONS = [
  { label: "Box", value: "BOX" },
  { label: "Envelope", value: "ENVELOPE" },
];

type BoxForm = { name: string; box_type: string; length_cm: string; width_cm: string; height_cm: string };
const blankBox = (): BoxForm => ({ name: "", box_type: "BOX", length_cm: "", width_cm: "", height_cm: "" });

type VEdit = { pieces: string; weight: string; box_id: string; location: string; shelf: string; position: string };
const EMPTY_VEDIT: VEdit = { pieces: "", weight: "", box_id: "", location: "", shelf: "", position: "" };

// Visual product browser: search the shop's products (photo · title · SKU · weight), edit
// pieces-per-box / weight / box per variant or bulk-apply to a multi-selection, and save —
// weights are written back to Shopify. Feeds the same per-SKU packing rules the AWB uses.
function ProductBrowser({ boxes, onSaved }: { boxes: PackingBox[]; onSaved: () => void }) {
  const [q, setQ] = useState("");
  const [products, setProducts] = useState<CatalogProduct[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [edits, setEdits] = useState<Record<string, VEdit>>({});
  const [dirty, setDirty] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulk, setBulk] = useState({ pieces: "", weight: "", box_id: "", location: "", shelf: "", position: "" });
  const [saving, setSaving] = useState(false);
  const [barcodes, setBarcodes] = useState<Record<string, string>>({});  // sku → current Shopify barcode
  const [barcodeBusy, setBarcodeBusy] = useState(false);
  const [labelOpts, setLabelOpts] = useState({ page: "A4", cols: "3", rows: "8", bar_height_mm: "12" });
  const metaRef = useRef<Record<string, { iid: string | null; title: string; image: string | null; pid: string; vid: string }>>({});

  const boxOptions = [{ label: "— none —", value: "" }, ...boxes.map((b) => ({ label: b.name, value: String(b.id) }))];

  const ingest = (prods: CatalogProduct[], append: boolean) => {
    setEdits((prev) => {
      const next = append ? { ...prev } : {};
      for (const p of prods) for (const v of p.variants) {
        if (!v.sku) continue;
        next[v.sku] = {
          pieces: v.pieces_per_parcel != null ? String(v.pieces_per_parcel) : "",
          weight: v.weight_kg != null ? String(v.weight_kg) : "",
          box_id: v.box_id != null ? String(v.box_id) : "",
          location: v.location ?? "",
          shelf: v.shelf ?? "",
          position: v.shelf_position ?? "",
        };
        metaRef.current[v.sku] = { iid: v.inventory_item_id, title: p.title, image: p.image, pid: p.id, vid: v.id };
      }
      return next;
    });
    setBarcodes((prev) => {
      const next = append ? { ...prev } : {};
      for (const p of prods) for (const v of p.variants) {
        if (v.sku) next[v.sku] = v.barcode ?? "";
      }
      return next;
    });
  };

  const search = async (reset = true) => {
    setLoading(true); setErr(null);
    try {
      const r = await getProducts({ q: q.trim() || undefined, cursor: reset ? undefined : cursor ?? undefined });
      setProducts((prev) => (reset ? r.products : [...prev, ...r.products]));
      ingest(r.products, !reset);
      setCursor(r.next_cursor);
      setLoaded(true);
      if (reset) { setDirty(new Set()); setSelected(new Set()); }
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Couldn't load products");
    } finally { setLoading(false); }
  };

  const setField = (sku: string, k: keyof VEdit, val: string) => {
    setEdits((prev) => ({ ...prev, [sku]: { ...(prev[sku] ?? EMPTY_VEDIT), [k]: val } }));
    setDirty((prev) => new Set(prev).add(sku));
  };
  const toggleSel = (sku: string) => setSelected((prev) => {
    const n = new Set(prev); if (n.has(sku)) n.delete(sku); else n.add(sku); return n;
  });

  const applyBulk = () => {
    if (selected.size === 0) return;
    setEdits((prev) => {
      const next = { ...prev };
      for (const sku of selected) {
        const cur = next[sku] ?? EMPTY_VEDIT;
        next[sku] = {
          pieces: bulk.pieces !== "" ? bulk.pieces : cur.pieces,
          weight: bulk.weight !== "" ? bulk.weight : cur.weight,
          box_id: bulk.box_id !== "" ? bulk.box_id : cur.box_id,
          location: bulk.location !== "" ? bulk.location : cur.location,
          shelf: bulk.shelf !== "" ? bulk.shelf : cur.shelf,
          position: bulk.position !== "" ? bulk.position : cur.position,
        };
      }
      return next;
    });
    setDirty((prev) => { const n = new Set(prev); for (const s of selected) n.add(s); return n; });
    toast(`Applied to ${selected.size} product(s) — review, then Save`);
  };

  const save = async () => {
    const items: ProductPackingItem[] = [];
    for (const sku of dirty) {
      const e = edits[sku]; if (!e) continue;
      const m = metaRef.current[sku] ?? { iid: null, title: sku, image: null };
      items.push({
        sku, inventory_item_id: m.iid, title: m.title, image_url: m.image,
        pieces_per_parcel: e.pieces === "" ? null : Number(e.pieces),
        weight_kg: e.weight === "" ? null : Number(e.weight),
        box_id: e.box_id === "" ? null : Number(e.box_id),
        location: e.location.trim() || null,
        shelf: e.shelf.trim() || null,
        shelf_position: e.position.trim() || null,
      });
    }
    if (items.length === 0) { toast("Nothing changed"); return; }
    setSaving(true);
    try {
      const r = await saveProductPacking(items);
      toast(`Saved ${r.saved}${r.weights_written ? ` · ${r.weights_written} weight(s) → Shopify` : ""}${r.errors.length ? ` · ${r.errors.length} weight error(s)` : ""}`, r.errors.length > 0);
      setDirty(new Set());
      onSaved();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Save failed", true);
    } finally { setSaving(false); }
  };

  // Generate an EAN-13 in Shopify for each SKU still missing a barcode.
  const genBarcodes = async (skus: string[]) => {
    const items = skus.map((sku) => {
      const m = metaRef.current[sku];
      if (!m?.pid || !m?.vid || (barcodes[sku] ?? "").trim()) return null;
      return { product_id: m.pid, variant_id: m.vid, sku, barcode: "" };
    }).filter(Boolean) as { product_id: string; variant_id: string; sku: string; barcode: string }[];
    if (items.length === 0) { toast("No products missing a barcode here."); return; }
    setBarcodeBusy(true);
    try {
      const r = await setProductBarcodes(items);
      setBarcodes((prev) => {
        const n = { ...prev };
        for (const res of r.results) if (res.sku) n[res.sku] = res.barcode ?? "";
        return n;
      });
      toast(`Generated ${r.results.length} barcode(s) → Shopify${r.errors.length ? ` · ${r.errors.length} failed` : ""}`, r.errors.length > 0);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Barcode generation failed", true);
    } finally { setBarcodeBusy(false); }
  };

  // Open a printable PDF of visual barcode labels to stick on products.
  const printLabels = async (skus: string[]) => {
    const labels = skus.map((sku) => {
      const bc = (barcodes[sku] ?? "").trim();
      return bc ? { barcode: bc, sku, title: metaRef.current[sku]?.title ?? "", copies: 1 } : null;
    }).filter(Boolean) as { barcode: string; sku: string; title: string; copies: number }[];
    if (labels.length === 0) { toast("No barcodes to print — generate them first.", true); return; }
    try {
      const blob = await productBarcodeLabels(labels, {
        page: labelOpts.page,
        cols: Number(labelOpts.cols) || 3,
        rows: Number(labelOpts.rows) || 8,
        bar_height_mm: Number(labelOpts.bar_height_mm) || 12,
      });
      const url = URL.createObjectURL(blob);
      window.open(url, "_blank");
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Couldn't build labels", true);
    }
  };

  const loadedSkus = products.flatMap((p) => p.variants.filter((v) => v.sku).map((v) => v.sku as string));
  const missingCount = loadedSkus.filter((s) => !(barcodes[s] ?? "").trim()).length;

  return (
    <BlockStack gap="300">
      <InlineStack gap="200" blockAlign="end">
        <div style={{ flex: 1 }}>
          <TextField label="Find products" labelHidden value={q} onChange={setQ} autoComplete="off" placeholder="Search products by title or SKU" />
        </div>
        <Button onClick={() => search(true)} loading={loading}>Search</Button>
        {dirty.size > 0 && <Button variant="primary" onClick={save} loading={saving}>{`Save ${dirty.size} change${dirty.size === 1 ? "" : "s"}`}</Button>}
      </InlineStack>

      {loaded && loadedSkus.length > 0 && (
        <BlockStack gap="200">
          <InlineStack gap="200" blockAlign="center" wrap>
            <Text as="span" tone="subdued" variant="bodySm">
              {`Barcodes: ${loadedSkus.length - missingCount}/${loadedSkus.length} set`}
            </Text>
            <Button size="slim" loading={barcodeBusy} disabled={missingCount === 0}
              onClick={() => void genBarcodes(loadedSkus)}>{`Generate ${missingCount} missing`}</Button>
            <Button size="slim" onClick={() => void printLabels(loadedSkus)}>Print all labels</Button>
          </InlineStack>
          <InlineStack gap="200" blockAlign="end" wrap>
            <Text as="span" tone="subdued" variant="bodySm">Label sheet:</Text>
            <div style={{ width: 120 }}>
              <Select label="Page" labelHidden value={labelOpts.page}
                onChange={(v) => setLabelOpts((o) => ({ ...o, page: v }))}
                options={[{ label: "A4", value: "A4" }, { label: "A5", value: "A5" }, { label: "Letter", value: "LETTER" }]} />
            </div>
            <div style={{ width: 84 }}><TextField label="Columns" type="number" value={labelOpts.cols}
              onChange={(v) => setLabelOpts((o) => ({ ...o, cols: v }))} autoComplete="off" /></div>
            <div style={{ width: 84 }}><TextField label="Rows" type="number" value={labelOpts.rows}
              onChange={(v) => setLabelOpts((o) => ({ ...o, rows: v }))} autoComplete="off" /></div>
            <div style={{ width: 110 }}><TextField label="Barcode height" type="number" suffix="mm"
              value={labelOpts.bar_height_mm} onChange={(v) => setLabelOpts((o) => ({ ...o, bar_height_mm: v }))} autoComplete="off" /></div>
          </InlineStack>
        </BlockStack>
      )}

      {err && (
        <Banner tone="critical" title="Couldn't load products">
          <p>{err}</p>
          <p>If you just enabled the inventory permission, re-open the app from Shopify admin to approve it.</p>
        </Banner>
      )}

      {selected.size > 0 && (
        <div style={{ border: "1px solid #c9cccf", borderRadius: 8, padding: 12, background: "#f6f6f7" }}>
          <BlockStack gap="200">
            <Text as="p" fontWeight="semibold">Apply to {selected.size} selected product{selected.size === 1 ? "" : "s"}</Text>
            <InlineStack gap="200" blockAlign="end">
              <div style={{ width: 120 }}><TextField label="Pieces/box" type="number" value={bulk.pieces} onChange={(v) => setBulk((b) => ({ ...b, pieces: v }))} autoComplete="off" /></div>
              <div style={{ width: 120 }}><TextField label="Weight (kg)" type="number" value={bulk.weight} onChange={(v) => setBulk((b) => ({ ...b, weight: v }))} autoComplete="off" /></div>
              <div style={{ width: 160 }}><Select label="Box" options={boxOptions} value={bulk.box_id} onChange={(v) => setBulk((b) => ({ ...b, box_id: v }))} /></div>
              <div style={{ width: 110 }}><TextField label="Location" value={bulk.location} onChange={(v) => setBulk((b) => ({ ...b, location: v }))} autoComplete="off" /></div>
              <div style={{ width: 90 }}><TextField label="Shelf" value={bulk.shelf} onChange={(v) => setBulk((b) => ({ ...b, shelf: v }))} autoComplete="off" /></div>
              <div style={{ width: 90 }}><TextField label="Position" value={bulk.position} onChange={(v) => setBulk((b) => ({ ...b, position: v }))} autoComplete="off" /></div>
              <Button onClick={applyBulk}>Apply</Button>
              <Button loading={barcodeBusy} onClick={() => void genBarcodes([...selected])}>Generate barcodes</Button>
              <Button onClick={() => void printLabels([...selected])}>Print labels</Button>
              <Button variant="plain" onClick={() => setSelected(new Set())}>Clear</Button>
            </InlineStack>
            <Text as="p" tone="subdued" variant="bodySm">Only the filled fields are applied. Review the rows, then Save.</Text>
          </BlockStack>
        </div>
      )}

      {!loaded && !loading ? (
        <Text as="p" tone="subdued">Search your products to set pieces-per-box, weight and box — one by one or bulk-select several and apply at once.</Text>
      ) : (
        <BlockStack gap="0">
          {products.map((p) => (
            <div key={p.id} style={{ borderTop: "1px solid #f1f1f1", padding: "10px 0" }}>
              <InlineStack gap="300" blockAlign="start" wrap={false}>
                {p.image ? <Thumbnail source={p.image} alt={p.title} size="small" /> : <div style={{ width: 40, height: 40, background: "#f1f1f1", borderRadius: 6 }} />}
                <div style={{ flex: 1 }}>
                  <BlockStack gap="150">
                    <Text as="span" fontWeight="semibold">{p.title}</Text>
                    {p.variants.filter((v) => v.sku).map((v) => {
                      const e = edits[v.sku as string] ?? EMPTY_VEDIT;
                      return (
                        <InlineStack key={v.id} gap="200" blockAlign="end" wrap>
                          <Checkbox label="" labelHidden checked={selected.has(v.sku as string)} onChange={() => toggleSel(v.sku as string)} />
                          <div style={{ width: 130 }}><Text as="span" tone="subdued" variant="bodySm">{v.sku}</Text></div>
                          <div style={{ width: 92 }}><TextField label="Pieces/box" labelHidden type="number" value={e.pieces} onChange={(val) => setField(v.sku as string, "pieces", val)} autoComplete="off" placeholder="pcs" suffix="/box" /></div>
                          <div style={{ width: 84 }}><TextField label="Weight" labelHidden type="number" value={e.weight} onChange={(val) => setField(v.sku as string, "weight", val)} autoComplete="off" placeholder="kg" suffix="kg" /></div>
                          <div style={{ width: 150 }}><Select label="Box" labelHidden options={boxOptions} value={e.box_id} onChange={(val) => setField(v.sku as string, "box_id", val)} /></div>
                          <div style={{ width: 100 }}><TextField label="Location" labelHidden value={e.location} onChange={(val) => setField(v.sku as string, "location", val)} autoComplete="off" placeholder="location" /></div>
                          <div style={{ width: 80 }}><TextField label="Shelf" labelHidden value={e.shelf} onChange={(val) => setField(v.sku as string, "shelf", val)} autoComplete="off" placeholder="shelf" /></div>
                          <div style={{ width: 80 }}><TextField label="Position" labelHidden value={e.position} onChange={(val) => setField(v.sku as string, "position", val)} autoComplete="off" placeholder="pos" /></div>
                          <div style={{ minWidth: 160 }}>
                            {(barcodes[v.sku as string] ?? "").trim() ? (
                              <InlineStack gap="150" blockAlign="center">
                                <Text as="span" variant="bodySm" tone="subdued">{barcodes[v.sku as string]}</Text>
                                <Button size="micro" onClick={() => void printLabels([v.sku as string])}>Label</Button>
                              </InlineStack>
                            ) : (
                              <Button size="micro" loading={barcodeBusy} onClick={() => void genBarcodes([v.sku as string])}>Generate barcode</Button>
                            )}
                          </div>
                        </InlineStack>
                      );
                    })}
                  </BlockStack>
                </div>
              </InlineStack>
            </div>
          ))}
          {cursor && <InlineStack align="center"><Button onClick={() => search(false)} loading={loading}>Load more</Button></InlineStack>}
        </BlockStack>
      )}
    </BlockStack>
  );
}

// In-app packing: define box types, then set pieces-per-box / box / weight PER PRODUCT so every
// order auto-splits into parcels and sends box size + weight to the courier. The automatic answer
// to xConnector's manual per-shipment package picking.
function PackingCard() {
  const [boxes, setBoxes] = useState<PackingBox[]>([]);
  const [rules, setRules] = useState<PackingRule[]>([]);
  const [settings, setSettings] = useState<AutomationSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [savingDefaults, setSavingDefaults] = useState(false);
  const [advanced, setAdvanced] = useState(false);
  const [boxEdit, setBoxEdit] = useState<number | null | undefined>(undefined);
  const [boxForm, setBoxForm] = useState<BoxForm>(blankBox());
  const [boxSaving, setBoxSaving] = useState(false);
  const [nr, setNr] = useState({ sku: "", pieces: "", weight: "", box_id: "" });
  const [ruleSaving, setRuleSaving] = useState(false);

  const refresh = async () => {
    const [b, r, sset] = await Promise.all([getBoxes(), getPackingRules(), getAutomationSettings()]);
    setBoxes(b.boxes); setRules(r.rules); setSettings(sset);
  };
  useEffect(() => { refresh().catch(() => {}).finally(() => setLoading(false)); }, []);

  const boxName = (id: number | null) => (id == null ? "—" : boxes.find((b) => b.id === id)?.name ?? `#${id}`);
  const boxOptions = [{ label: "— none —", value: "" },
    ...boxes.map((b) => ({ label: `${b.name}${b.box_type === "ENVELOPE" ? " (envelope)" : ""}`, value: String(b.id) }))];
  const patchSettings = (patch: Partial<AutomationSettings>) => setSettings((sx) => (sx ? { ...sx, ...patch } : sx));

  const saveDefaults = async () => {
    if (!settings) return;
    setSavingDefaults(true);
    try {
      await updateAutomationSettings({
        default_pieces_per_parcel: settings.default_pieces_per_parcel,
        packing_rounding: settings.packing_rounding,
        default_box_id: settings.default_box_id,
        packing_metafield: settings.packing_metafield,
        packing_per_product_tag: settings.packing_per_product_tag,
      });
      toast("Packing settings saved");
    } catch (e) { toast(e instanceof Error ? e.message : "Save failed", true); }
    finally { setSavingDefaults(false); }
  };

  const openNewBox = () => { setBoxForm(blankBox()); setBoxEdit(null); };
  const openEditBox = (b: PackingBox) => {
    setBoxForm({ name: b.name, box_type: b.box_type,
      length_cm: b.length_cm ? String(b.length_cm) : "", width_cm: b.width_cm ? String(b.width_cm) : "",
      height_cm: b.height_cm ? String(b.height_cm) : "" });
    setBoxEdit(b.id);
  };
  const saveBox = async () => {
    if (!boxForm.name.trim()) { toast("Box name required", true); return; }
    setBoxSaving(true);
    try {
      const input: PackingBoxInput = { name: boxForm.name.trim(), box_type: boxForm.box_type,
        length_cm: boxForm.length_cm ? Number(boxForm.length_cm) : null,
        width_cm: boxForm.width_cm ? Number(boxForm.width_cm) : null,
        height_cm: boxForm.height_cm ? Number(boxForm.height_cm) : null };
      if (boxEdit == null) await createBox(input); else await updateBox(boxEdit, input);
      await refresh(); setBoxEdit(undefined); toast("Box saved");
    } catch (e) { toast(e instanceof Error ? e.message : "Save failed", true); }
    finally { setBoxSaving(false); }
  };
  const removeBox = async (id: number) => {
    try { await deleteBox(id); await refresh(); toast("Box deleted"); }
    catch (e) { toast(e instanceof Error ? e.message : "Delete failed", true); }
  };

  const addRule = async () => {
    if (!nr.sku.trim()) { toast("SKU required", true); return; }
    setRuleSaving(true);
    try {
      await upsertPackingRules([{ sku: nr.sku.trim(),
        pieces_per_parcel: nr.pieces ? Number(nr.pieces) : null,
        weight_kg: nr.weight ? Number(nr.weight) : null,
        box_id: nr.box_id ? Number(nr.box_id) : null }]);
      setNr({ sku: "", pieces: "", weight: "", box_id: "" });
      await refresh(); toast("Product packing saved");
    } catch (e) { toast(e instanceof Error ? e.message : "Save failed", true); }
    finally { setRuleSaving(false); }
  };
  const removeRule = async (id: number) => {
    try { await deletePackingRule(id); await refresh(); toast("Removed"); }
    catch (e) { toast(e instanceof Error ? e.message : "Delete failed", true); }
  };

  if (loading || !settings) {
    return (<Card><BlockStack gap="300"><Text as="h2" variant="headingMd">Parcel packing</Text><SkeletonBodyText lines={6} /></BlockStack></Card>);
  }

  return (
    <Card>
      <BlockStack gap="400">
        <Text as="h2" variant="headingMd">Parcel packing (automatic)</Text>
        <Text as="p" tone="subdued">
          Tell the app how your products pack — pieces per box, box size, unit weight — per product.
          Every order (manual and automatic) then splits into the right number of parcels and sends the
          box size + weight to the courier. No picking per shipment.
        </Text>

        <BlockStack gap="200">
          <InlineStack align="space-between" blockAlign="center">
            <Text as="h3" variant="headingSm">Box types</Text>
            {boxEdit === undefined && <Button onClick={openNewBox}>Add box</Button>}
          </InlineStack>
          {boxEdit === undefined && boxes.length === 0 ? (
            <Text as="p" tone="subdued">No boxes yet. Add a box or envelope with its dimensions.</Text>
          ) : boxEdit === undefined ? (
            <ResourceList resourceName={{ singular: "box", plural: "boxes" }} items={boxes}
              renderItem={(b) => (
                <ResourceItem id={String(b.id)} onClick={() => openEditBox(b)}>
                  <InlineStack align="space-between" blockAlign="center">
                    <Text as="span" fontWeight="semibold">
                      {b.name} <Text as="span" tone="subdued">· {b.box_type === "ENVELOPE" ? "envelope" : "box"} · {b.length_cm ?? "–"}×{b.width_cm ?? "–"}×{b.height_cm ?? "–"} cm</Text>
                    </Text>
                    <InlineStack gap="200">
                      <Button onClick={() => openEditBox(b)}>Edit</Button>
                      <Button tone="critical" variant="plain" onClick={() => removeBox(b.id)}>Delete</Button>
                    </InlineStack>
                  </InlineStack>
                </ResourceItem>
              )} />
          ) : (
            <BlockStack gap="300">
              <InlineStack gap="300">
                <div style={{ flex: 2 }}><TextField label="Box name" value={boxForm.name} onChange={(v) => setBoxForm((f) => ({ ...f, name: v }))} autoComplete="off" placeholder="Small box" /></div>
                <div style={{ flex: 1 }}><Select label="Type" options={BOX_TYPE_OPTIONS} value={boxForm.box_type} onChange={(v) => setBoxForm((f) => ({ ...f, box_type: v }))} /></div>
              </InlineStack>
              <InlineStack gap="300">
                <div style={{ flex: 1 }}><TextField label="Length (cm)" type="number" value={boxForm.length_cm} onChange={(v) => setBoxForm((f) => ({ ...f, length_cm: v }))} autoComplete="off" /></div>
                <div style={{ flex: 1 }}><TextField label="Width (cm)" type="number" value={boxForm.width_cm} onChange={(v) => setBoxForm((f) => ({ ...f, width_cm: v }))} autoComplete="off" /></div>
                <div style={{ flex: 1 }}><TextField label="Height (cm)" type="number" value={boxForm.height_cm} onChange={(v) => setBoxForm((f) => ({ ...f, height_cm: v }))} autoComplete="off" /></div>
              </InlineStack>
              <InlineStack gap="200">
                <Button variant="primary" onClick={saveBox} loading={boxSaving}>Save box</Button>
                <Button onClick={() => setBoxEdit(undefined)}>Cancel</Button>
              </InlineStack>
            </BlockStack>
          )}
        </BlockStack>

        <Divider />

        <BlockStack gap="300">
          <Text as="h3" variant="headingSm">Defaults</Text>
          <InlineStack gap="300">
            <div style={{ flex: 1 }}><TextField label="Default pieces per box" type="number" value={settings.default_pieces_per_parcel != null ? String(settings.default_pieces_per_parcel) : ""} onChange={(v) => patchSettings({ default_pieces_per_parcel: v === "" ? null : Number(v) })} autoComplete="off" helpText="For products with no rule below." /></div>
            <div style={{ flex: 1 }}><Select label="Default box" options={boxOptions} value={settings.default_box_id != null ? String(settings.default_box_id) : ""} onChange={(v) => patchSettings({ default_box_id: v ? Number(v) : null })} helpText="Applied when a product has no box set." /></div>
          </InlineStack>
          <Select label="When an order has several different products" options={ROUNDING_OPTIONS} value={settings.packing_rounding || "shared"} onChange={(v) => patchSettings({ packing_rounding: v })} helpText="Share = fewest parcels (products share boxes). Per product = each product rounds up on its own." />
          <InlineStack><Button variant="primary" onClick={saveDefaults} loading={savingDefaults}>Save defaults</Button></InlineStack>
        </BlockStack>

        <Divider />

        <BlockStack gap="300">
          <Text as="h3" variant="headingSm">Per-product packing</Text>
          <Text as="p" tone="subdued">Set pieces-per-box, box and weight per product. Editing weight writes it back to Shopify. Overrides the defaults above.</Text>
          <ProductBrowser boxes={boxes} onSaved={refresh} />
          <Divider />
          {rules.length > 0 && (
            <ResourceList resourceName={{ singular: "product", plural: "products" }} items={rules}
              renderItem={(r) => (
                <ResourceItem id={String(r.id)} onClick={() => {}}
                  media={r.image_url ? <Thumbnail source={r.image_url} alt={r.title ?? r.sku} size="small" /> : undefined}>
                  <InlineStack align="space-between" blockAlign="center">
                    <BlockStack gap="050">
                      <Text as="span" fontWeight="semibold">{r.title ?? r.sku}</Text>
                      <Text as="span" tone="subdued">{r.sku} · {r.pieces_per_parcel ?? "–"} pcs/box · {r.weight_kg ?? "–"} kg · {boxName(r.box_id)}</Text>
                    </BlockStack>
                    <Button tone="critical" variant="plain" onClick={() => removeRule(r.id)}>Remove</Button>
                  </InlineStack>
                </ResourceItem>
              )} />
          )}
          <Text as="p" fontWeight="semibold" variant="bodySm">Add a packing rule by SKU (advanced)</Text>
          <Text as="p" tone="subdued" variant="bodySm">Only for SKUs not in your Shopify catalog. This just sets packing for that SKU — it does NOT create a product or add anything to an order.</Text>
          <InlineStack gap="200" blockAlign="end">
            <div style={{ flex: 2 }}><TextField label="SKU" value={nr.sku} onChange={(v) => setNr((n) => ({ ...n, sku: v }))} autoComplete="off" placeholder="HA-0002" /></div>
            <div style={{ flex: 1 }}><TextField label="Pieces/box" type="number" value={nr.pieces} onChange={(v) => setNr((n) => ({ ...n, pieces: v }))} autoComplete="off" /></div>
            <div style={{ flex: 1 }}><TextField label="Weight (kg)" type="number" value={nr.weight} onChange={(v) => setNr((n) => ({ ...n, weight: v }))} autoComplete="off" /></div>
            <div style={{ flex: 1 }}><Select label="Box" options={boxOptions} value={nr.box_id} onChange={(v) => setNr((n) => ({ ...n, box_id: v }))} /></div>
            <Button variant="primary" onClick={addRule} loading={ruleSaving}>Add</Button>
          </InlineStack>
        </BlockStack>

        <Divider />

        <Button variant="plain" disclosure={advanced ? "up" : "down"} onClick={() => setAdvanced((v) => !v)}>
          {advanced ? "Hide advanced" : "Advanced (Shopify metafield · tag)"}
        </Button>
        {advanced && (
          <BlockStack gap="300">
            <TextField label="Read pieces-per-parcel from a Shopify metafield" value={settings.packing_metafield ?? ""} onChange={(v) => patchSettings({ packing_metafield: v })} autoComplete="off" placeholder="custom.pieces_per_parcel" helpText="Optional — only used when no in-app rule/default applies. Value = parcels PER PIECE (0.1 = 10 pieces per parcel)." />
            <TextField label="Per-product rounding tag" value={settings.packing_per_product_tag ?? ""} onChange={(v) => patchSettings({ packing_per_product_tag: v })} autoComplete="off" placeholder="per_product" helpText="An order with this tag rounds per product regardless of the default." />
            <InlineStack><Button onClick={saveDefaults} loading={savingDefaults}>Save advanced</Button></InlineStack>
          </BlockStack>
        )}
      </BlockStack>
    </Card>
  );
}


/** Turn the sandbox on or off. Everything it seeds is deleted again when it's switched off. */
function TestModeCard() {
  const [on, setOn] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => { getTestMode().then((r) => setOn(!!r.test_mode)).catch(() => setOn(false)); }, []);

  const flip = async (next: boolean) => {
    if (next && !confirm(
      "Turn on test mode?\n\nThis creates demo stores, orders, courier accounts, packing rules and picking lists so you can try every feature. Your real courier accounts stop working until you turn it off.")) return;
    if (!next && !confirm(
      "Leave test mode?\n\nAll demo data is deleted. Your real data isn't touched.")) return;
    setBusy(true); setMsg(null);
    try {
      const r = await setTestMode(next);
      setOn(!!r.test_mode);
      const counts = r.created ?? r.removed ?? {};
      const summary = Object.entries(counts).map(([k, v]) => `${v} ${k.replace(/_/g, " ")}`).join(" · ");
      setMsg(next ? `Sandbox ready — ${summary}` : `Demo data removed — ${summary}`);
      setTimeout(() => location.reload(), 900);
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : "Couldn't change test mode");
    } finally { setBusy(false); }
  };

  return (
    <Card>
      <BlockStack gap="300">
        <InlineStack align="space-between" blockAlign="center">
          <Text as="h2" variant="headingMd">Test mode</Text>
          {on && <Badge tone="warning">On</Badge>}
        </InlineStack>
        <Text as="p" tone="subdued" variant="bodyMd">
          A full sandbox: three fake courier accounts that really issue AWBs and printable labels,
          fake invoices, demo stores, demo orders in every state (to ship, in transit, delivered,
          refused, bad address, duplicates), packing rules, product barcodes and picking lists — so
          you or a reviewer can try every feature before connecting anything real.
        </Text>
        <Text as="p" tone="subdued" variant="bodySm">
          While it's on, <b>your real courier accounts are refused</b>, so no real parcel can be
          booked. Your real orders stay visible and editable — and changes to them are real.
        </Text>
        {msg && <Banner tone={on ? "success" : "info"}>{msg}</Banner>}
        <InlineStack gap="200">
          <Button variant={on ? undefined : "primary"} tone={on ? "critical" : undefined}
            loading={busy} disabled={on === null} onClick={() => void flip(!on)}>
            {on ? "Turn off test mode" : "Turn on test mode"}
          </Button>
          {on && (
            <Button onClick={() => {
              try { localStorage.removeItem(COACH_KEY); } catch { /* private mode */ }
              setMsg("The page-by-page guide is back — visit any page to see it.");
            }}>
              Show the guide again
            </Button>
          )}
        </InlineStack>
      </BlockStack>
    </Card>
  );
}


/** App language. English is the source language; Romanian covers the app shell and falls back to
 *  English anywhere it isn't translated yet, so nothing ever renders blank. */
function LanguageCard() {
  const { lang, setLang, t } = useLang();
  return (
    <Card>
      <BlockStack gap="300">
        <Text as="h2" variant="headingMd">{t("Language")}</Text>
        <Text as="p" tone="subdued" variant="bodyMd">
          Order Hub follows your Shopify admin's language the first time you open it. Change it here
          any time — the choice is remembered on this device.
        </Text>
        <InlineStack gap="200">
          <ButtonGroup variant="segmented">
            <Button pressed={lang === "en"} onClick={() => setLang("en")}>{t("English")}</Button>
            <Button pressed={lang === "ro"} onClick={() => setLang("ro")}>{t("Romanian")}</Button>
          </ButtonGroup>
        </InlineStack>
        <Text as="p" tone="subdued" variant="bodySm">
          Romanian currently covers the command console, test mode and the home screen; the rest of
          the app stays in English until it's translated. Sidekick understands both regardless.
        </Text>
      </BlockStack>
    </Card>
  );
}

export default function Settings() {
  const [data, setData] = useState<CouriersResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searchParams, setSearchParams] = useSearchParams();

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
  const mappings = data?.mappings ?? [];

  const courierAccountsCard = (
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
  );

  const courierMappingsCard = (
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
  );

  /* One long scroll became six tabs. The tab id lives in ?tab= so the Home setup guide can deep-link
     straight to the step it's asking for, and a reload keeps you where you were. */
  const TABS: { id: string; content: string; body: React.ReactNode }[] = [
    { id: "couriers", content: "Couriers",
      body: <BlockStack gap="400">{courierAccountsCard}{courierMappingsCard}</BlockStack> },
    { id: "shipping", content: "Shipping",
      body: <BlockStack gap="400"><ShipmentProfilesCard /><PackingCard /><ShipmentRulesCard /></BlockStack> },
    { id: "automation", content: "Automation", body: <BlockStack gap="400"><AutomationCard /><AutomationScheduleCard /></BlockStack> },
    { id: "invoicing", content: "Invoicing", body: <InvoiceSettingsCard /> },
    { id: "organization", content: "Organization", body: <OrganizationCard /> },
    { id: "testing", content: "Test mode", body: <TestModeCard /> },
    { id: "language", content: "Language", body: <LanguageCard /> },
  ];
  const tabIndex = Math.max(0, TABS.findIndex((t) => t.id === (searchParams.get("tab") || "couriers")));

  return (
    <Page fullWidth title="Settings" subtitle="Couriers, shipping, automation, invoicing and multi-store.">
      <BlockStack gap="400">
        {error && (
          <Banner tone="critical" title="Couldn't load settings" onDismiss={() => setError(null)}>
            <p>{error}</p>
          </Banner>
        )}
        <Card padding="0">
          <Tabs
            tabs={TABS.map((t) => ({ id: t.id, content: t.content }))}
            selected={tabIndex}
            onSelect={(i) => setSearchParams({ tab: TABS[i].id }, { replace: true })}
          />
        </Card>
        {TABS[tabIndex].body}
      </BlockStack>
    </Page>
  );
}

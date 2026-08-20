/** i18n unificat EN/RO — DOUĂ API-uri peste același dicționar:
 *  • React: <LanguageProvider> + useLang() → { lang, setLang, t } (App-ul îl montează la rădăcină);
 *  • standalone: getLang()/setLang()/t() pentru module-level const-uri (ex. ALL_COLUMNS în Orders).
 * setLang persistă + dă reload, ca evaluările module-level să rămână corecte — simplu și consistent.
 * Chei: fie cod-style ("col.order"), fie SELF-KEYING (cheia = textul englez, ex. t("Clear")) — o cheie
 * lipsă cade pe cheie, deci engleza merge automat; în STR adăugăm doar traducerea RO. */
import React, { createContext, useContext } from "react";

export type Lang = "en" | "ro";
const KEY = "orderhub.lang";

export function getLang(): Lang {
  try {
    const saved = localStorage.getItem(KEY);
    if (saved === "ro" || saved === "en") return saved;
  } catch { /* private mode */ }
  const url = new URLSearchParams(window.location.search).get("locale") || "";
  if (url.toLowerCase().startsWith("ro")) return "ro";
  if ((navigator.language || "").toLowerCase().startsWith("ro")) return "ro";
  return "en";
}

export function setLang(l: Lang) {
  try { localStorage.setItem(KEY, l); } catch { /* private mode */ }
  window.location.reload();
}

const L: Lang = getLang();

const STR: Record<string, { en?: string; ro: string }> = {
  // ── Orders: lens tabs ──
  "lens.all": { en: "All", ro: "Toate" },
  "lens.unfulfilled": { en: "Unfulfilled", ro: "Neexpediate" },
  "lens.fulfilled": { en: "Fulfilled", ro: "Expediate" },
  "lens.in_transit": { en: "In transit", ro: "În tranzit" },
  "lens.delivered": { en: "Delivered", ro: "Livrate" },
  "lens.refused": { en: "Refused", ro: "Refuzate" },
  // ── Orders: coloane & views ──
  "cols.button": { en: "Columns", ro: "Coloane" },
  "cols.view": { en: "View", ro: "View" },
  "cols.compact": { en: "Compact", ro: "Compact" },
  "cols.default": { en: "Default", ro: "Standard" },
  "cols.detailed": { en: "Detailed", ro: "Detaliat" },
  "views.saved": { en: "Saved views", ro: "View-uri salvate" },
  "views.name": { en: "View name…", ro: "Nume view…" },
  "views.save": { en: "Save", ro: "Salvează" },
  "cols.show": { en: "Show these columns (↑↓ = order)", ro: "Alege coloanele (↑↓ = ordinea)" },
  "col.order": { en: "Order", ro: "Comandă" },
  "col.status": { en: "Status", ro: "Status" },
  "col.payment": { en: "Payment", ro: "Plată" },
  "col.fulfilment": { en: "Fulfilment", ro: "Expediere" },
  "col.store": { en: "Store", ro: "Magazin" },
  "col.customer": { en: "Customer", ro: "Client" },
  "col.date": { en: "Date", ro: "Dată" },
  "col.total": { en: "Total", ro: "Total" },
  "col.products": { en: "Products", ro: "Produse" },
  "col.address": { en: "Address", ro: "Adresă" },
  "col.awb": { en: "Courier / AWB", ro: "Curier / AWB" },
  "col.invoice": { en: "Invoice", ro: "Factură" },
  "col.actions": { en: "Actions", ro: "Acțiuni" },
  // ── Orders: rând expandat ──
  "exp.products": { en: "Products", ro: "Produse" },
  "exp.address": { en: "Address", ro: "Adresă" },
  "exp.state": { en: "Status", ro: "Stare" },
  "exp.invoice": { en: "Invoice", ro: "Factură" },
  "exp.total": { en: "Total", ro: "Total" },
  "exp.edit": { en: "Edit order", ro: "Modifică comanda" },
  "pay.pending": { en: "Payment pending", ro: "Plată în așteptare" },
  "count.showing": { en: "{n} orders in this view", ro: "{n} comenzi în acest view" },
  "count.filtered": { en: "filtered", ro: "filtrat" },
  "date.from": { en: "From", ro: "De la" },
  "date.to": { en: "To", ro: "Până la" },
  "date.apply": { en: "Apply", ro: "Aplică" },
  "date.clear": { en: "Clear", ro: "Șterge" },
  "note.title": { en: "Order note", ro: "Notă comandă" },
  "note.placeholder": { en: "Add a note — it is saved on the Shopify order.", ro: "Adaugă o notă — se salvează pe comanda din Shopify." },
  // ── Programare automatizări ──
  "sched.title": { en: "Automation schedule", ro: "Programare automatizări" },
  "sched.desc": {
    en: "Choose WHEN each automation runs: on order (immediately or after X min), periodically (cron), or on delivery (COD). Everything runs in shadow mode (log-only) until go-live.",
    ro: "Alege CÂND rulează fiecare automatizare: la comandă (imediat sau după X min), periodic (cron), sau la livrare (COD). Totul rulează în modul shadow (log-only) până la go-live.",
  },
  "sched.loading": { en: "Loading…", ro: "Se încarcă…" },
  "sched.save": { en: "Save", ro: "Salvează" },
  "sched.saved": { en: "Schedule saved", ro: "Programare salvată" },
  "sched.savefail": { en: "Couldn't save", ro: "Nu s-a putut salva" },
  "sched.mode": { en: "Mode", ro: "Mod" },
  "sched.min": { ro: "min", en: "min" },
  "sched.every": { en: "Every (min)", ro: "Interval (min)" },
  "sched.after": { en: "Min. after order", ro: "Min. după comandă" },
  "mode.on_order": { en: "On order", ro: "La comandă" },
  "mode.cron": { en: "Cron (periodic)", ro: "Cron (periodic)" },
  "mode.on_delivered": { en: "On delivery", ro: "La livrare" },
  "mode.off": { en: "Off", ro: "Oprit" },
  "auto.duplicates": { en: "Duplicates", ro: "Duplicate" },
  "auto.duplicates.help": { en: "Duplicate orders from the same customer — organization-wide.", ro: "Comenzi dublate ale aceluiași client — la nivel de organizație." },
  "auto.parcels": { en: "Parcel count", ro: "Nr. colete" },
  "auto.parcels.help": { en: "How many parcels the AWB gets (memoized for AWB).", ro: "Câte colete are AWB-ul (memorat pentru AWB)." },
  "auto.surprise": { en: "Surprise (perfume)", ro: "Surpriză (parfum)" },
  "auto.surprise.help": { en: "Only Esteban / George Talent / Lab Noir / Nubra.", ro: "Doar Esteban / George Talent / Lab Noir / Nubra." },
  "auto.blocklist": { en: "Blocklist / serial refuser", ro: "Blocklist / serial-refuser" },
  "auto.blocklist.help": { en: "Manually blocked customer or ≥2 refusals in the group. International → cancel; RO with CS → hold.", ro: "Client blocat manual sau cu ≥2 refuzuri în grup. Pe internațional → anulează; pe RO cu CS → hold." },
  "auto.special": { en: "Special rules", ro: "Reguli speciale" },
  "auto.special.help": { en: "Keyword in tag/note → action (see the list below).", ro: "Keyword în tag/notă → acțiune (vezi lista de mai jos)." },
  "auto.cod_capture": { en: "COD capture", ro: "COD capture" },
  "auto.cod_capture.help": { en: "On delivery: mark paid / tag refused.", ro: "La livrare: marchează plătit / tag refuzat." },
  "auto.awb": { en: "AWB", ro: "AWB" },
  "auto.awb.help": { en: "Creates the label (only inside the AWB window).", ro: "Creează eticheta (doar în fereastra AWB)." },
  "nohold.title": { en: "No holds (international)", ro: "Fără hold-uri (internațional)" },
  "nohold.label": { en: "Don't leave orders on hold — try to ship everything; what can't ship → cancel", ro: "Nu lăsa comenzi pe hold — încearcă să trimiți tot; ce nu se poate → anulează" },
  "nohold.help": { en: "For stores without CS (international): any hold (medium risk / different-total duplicate) becomes ship; blocked customers / impossible address → cancel.", ro: "Pentru magazine fără CS (internaționale): orice hold (duplicat cu sumă diferită) devine trimite; clienții blocați / adresă imposibilă → anulare." },
  "fulfill.title": { en: "Mark as fulfilled in Shopify", ro: "Când apare expediată în Shopify" },
  "fulfill.on_pickup": { en: "When the courier picks it up (recommended)", ro: "Când o preia curierul (recomandat)" },
  "fulfill.on_label": { en: "As soon as the label is created", ro: "Imediat ce se face eticheta" },
  "fulfill.help": {
    en: "Only for direct couriers (DPD, Sameday, GLS…), where Order Hub pushes the fulfillment. xConnector and Frisbo fulfill the order themselves. Picking 'as soon as the label is created' can notify the customer before the parcel actually leaves.",
    ro: "Doar la curierii direcți (DPD, Sameday, GLS…), unde Order Hub împinge fulfillment-ul. xConnector și Frisbo fulfill-uiesc singure comanda. Dacă alegi varianta cu eticheta, clientul poate fi notificat înainte ca pachetul să plece efectiv." },
  "rules.title": { en: "Special rules", ro: "Reguli speciale" },
  "rules.desc": { en: "If the order's tag or note contains a keyword → action, OVER the default policy. E.g. influencer → hold (even international). The customer name is encrypted, so only tag/note are searched.", ro: "Dacă tag-ul sau nota comenzii conține un cuvânt → acțiune, PESTE politica implicită. Ex: influencer → hold (chiar și pe internațional). Numele clientului e criptat, deci se caută doar în tag/notă." },
  "rules.contains": { en: "Contains", ro: "Conține" },
  "rules.action": { en: "Action", ro: "Acțiune" },
  "rules.add": { en: "+ Add rule", ro: "+ Adaugă regulă" },
  "rules.remove": { en: "Remove", ro: "Șterge" },
  "act.hold": { en: "Hold", ro: "Hold" },
  "act.cancel": { en: "Cancel", ro: "Anulare" },
  "act.ship": { en: "Ship", ro: "Trimite" },
  // ── AddressLab (Verifică o adresă) ──
  "Check an address": { ro: "Verifică o adresă" },
  "Check": { ro: "Verifică" },
  "Country": { ro: "Țara" },
  "County / region": { ro: "Județ / regiune" },
  "Locality": { ro: "Localitate" },
  "Postal code": { ro: "Cod poștal" },
  "Address 1 (street + number)": { ro: "Adresa 1 (stradă + număr)" },
  "Address 2": { ro: "Adresa 2" },
  "Czechia (CZ)": { ro: "Cehia (CZ)" },
  "Poland (PL)": { ro: "Polonia (PL)" },
  "Hungary (HU)": { ro: "Ungaria (HU)" },
  "Slovakia (SK)": { ro: "Slovacia (SK)" },
  "Other country (→ geocoder)": { ro: "Altă țară (→ geocoder)" },
  "VALID — ships as-is": { ro: "VALID — pleacă așa cum e" },
  "CORRECTED — write-back proposed": { ro: "CORECTAT — write-back propus" },
  "GEOCODER — the nomenclator cannot decide, goes to HERE": { ro: "GEOCODER — nomenclatorul nu decide, merge la HERE" },
  "CS — needs a human": { ro: "CS — are nevoie de om" },
  "The check failed.": { ro: "Verificarea a eșuat." },
  "Could not load the rules.": { ro: "Nu am putut încărca regulile." },
  "Could not load the rules": { ro: "Nu am putut încărca regulile" },
  "Saving failed": { ro: "Salvarea a eșuat" },
  "Save policies": { ro: "Salvează politicile" },
  "Corrections (CS backlog)": { ro: "Corecții (CS backlog)" },
  "The consolidated validator: check an address, see the rules, tune the policies.": { ro: "Validatorul consolidat: verifică o adresă, vezi regulile, ajustează politicile." },
  "International (CZ/PL/BG/HU/SK)": { ro: "Internațional (CZ/PL/BG/HU/SK)" },
  "source": { ro: "sursă" },
  // ── Chei self-keying (cheia = textul EN) folosite de consolă/Home/Settings ──
  "Language": { ro: "Limbă" },
  "English": { ro: "Engleză" },
  "Romanian": { ro: "Română" },
  "Clear": { ro: "Golește" },
  "Command": { ro: "Comandă" },
  "Examples": { ro: "Exemple" },
  "Open Orders": { ro: "Deschide Comenzi" },
  "Order Hub console": { ro: "Consola Order Hub" },
  "opens / closes the console": { ro: "deschide / închide consola" },
  "e.g. refused yesterday · awb-all payment:cod": { ro: "ex: refuzate ieri · awb-all payment:cod" },
  "Filter, find and act — from any page": { ro: "Filtrează, caută și acționează — de pe orice pagină" },
  "Ask Sidekick about your orders — it answers from Order Hub.": { ro: "Întreabă Sidekick despre comenzile tale — răspunde din Order Hub." },
  "Type a command — the answer stays here, so you can carry on with the next one.": { ro: "Scrie o comandă — răspunsul rămâne aici, ca să poți continua cu următoarea." },
  "Works in Romanian or English. To act, use the ⌘ console on any page.": { ro: "Merge în română sau engleză. Ca să acționezi, folosește consola ⌘ de pe orice pagină." },
  "You can combine them: filter first, then ask for the action. Every action asks for confirmation.": { ro: "Le poți combina: întâi filtrezi, apoi ceri acțiunea. Orice acțiune cere confirmare." },
};

export function t(key: string): string {
  const e = STR[key];
  if (!e) return key;                    // self-keying: cheia = textul EN
  return (L === "ro" ? e.ro : e.en) ?? key;
}

// ── API-ul React (LanguageProvider + useLang) — același dicționar ──
type LangCtx = { lang: Lang; setLang: (l: Lang) => void; t: (k: string) => string };
const Ctx = createContext<LangCtx>({ lang: L, setLang, t });

export function LanguageProvider({ children }: { children: React.ReactNode }) {
  // setLang persistă + reload → valoarea din context e stabilă pe viața paginii.
  return React.createElement(Ctx.Provider, { value: { lang: L, setLang, t } }, children);
}

export function useLang(): LangCtx {
  return useContext(Ctx);
}

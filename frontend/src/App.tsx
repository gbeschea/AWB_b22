import { useEffect, useState } from "react";
import { Routes, Route, Navigate, Link } from "react-router-dom";
import { NavMenu } from "@shopify/app-bridge-react";
import { Banner, Button } from "@shopify/polaris";
import { getMe } from "./lib/api";
import { CommandBusProvider } from "./lib/commandBus";
import { LanguageProvider } from "./lib/i18n";
import CommandBubble from "./components/CommandBubble";
import TestModeBanner from "./components/TestModeBanner";
import TestModeCoach from "./components/TestModeCoach";

import Home from "./pages/Home";
import Orders from "./pages/Orders";
import Scan from "./pages/Scan";
import CSQueue from "./pages/CSQueue";
import Picking from "./pages/Picking";
import Validation from "./pages/Validation";
import AddressLab from "./pages/AddressLab";
import Printing from "./pages/Printing";
import Billing from "./pages/Billing";
import Settings from "./pages/Settings";

/**
 * App shell: the App Bridge <NavMenu> declares the top-level admin nav (rendered by
 * Shopify in the admin chrome, outside the iframe). The first link MUST have
 * rel="home". React Router renders the matching screen inside the iframe. Because the
 * app is mounted at /app, both the NavMenu hrefs and the routes use the /app prefix.
 */
export default function App() {
  return (
    <LanguageProvider>
    <CommandBusProvider>
      <NavMenu>
        <Link to="/app" rel="home">
          Home
        </Link>
        <Link to="/app/orders">Orders</Link>
        <Link to="/app/cs-queue">CS backlog</Link>
        <Link to="/app/validation">Address validation</Link>
        <Link to="/app/address-lab">Address lab</Link>
        <Link to="/app/picking">Picking</Link>
        <Link to="/app/printing">Printing</Link>
        <Link to="/app/scan">Scan</Link>
        <Link to="/app/settings">Settings</Link>
        <Link to="/app/billing">Plans</Link>
      </NavMenu>

      <TestModeBanner />
      <TestModeCoach />
      <ScopeBanner />

      <Routes>
        <Route path="/app" element={<Home />} />
        {/* Overview was merged into Home — keep old links/bookmarks working. */}
        <Route path="/app/overview" element={<Navigate to="/app" replace />} />
        <Route path="/app/orders" element={<Orders />} />
        <Route path="/app/cs-queue" element={<CSQueue />} />
        <Route path="/app/picking" element={<Picking />} />
        <Route path="/app/validation" element={<Validation />} />
        <Route path="/app/address-lab" element={<AddressLab />} />
        <Route path="/app/printing" element={<Printing />} />
        <Route path="/app/scan" element={<Scan />} />
        <Route path="/app/settings" element={<Settings />} />
        <Route path="/app/billing" element={<Billing />} />
        {/* Any unknown path (e.g. a bare "/") lands on Home. */}
        <Route path="*" element={<Navigate to="/app" replace />} />
      </Routes>

      {/* One command console, docked on every page. */}
      <CommandBubble />
    </CommandBusProvider>
    </LanguageProvider>
  );
}

const SCOPE_LABELS: Record<string, string> = {
  write_inventory: "edit product weight",
  write_order_edits: "edit order products, discounts & shipping",
  write_products: "generate & write product barcodes",
};

/**
 * When the app requires scopes the shop hasn't granted yet (e.g. new features added after
 * install), show a re-consent prompt. OAuth can't run inside the iframe, so the button breaks
 * out to top-level with target="_top".
 */
function ScopeBanner() {
  const [missing, setMissing] = useState<string[]>([]);
  const [url, setUrl] = useState<string | null>(null);

  useEffect(() => {
    getMe().then((me) => { setMissing(me.missing_scopes ?? []); setUrl(me.reauth_url ?? null); })
      .catch(() => { /* ignore */ });
  }, []);

  if (missing.length === 0 || !url) return null;
  const labels = missing.map((s) => SCOPE_LABELS[s] || s).join(", ");
  return (
    <div style={{ padding: "12px 16px 0" }}>
      <Banner tone="warning" title="New permissions needed">
        <p>To use the newest features ({labels}), the app needs additional permissions. Approve them once to enable them.</p>
        <div style={{ marginTop: 8 }}>
          <Button url={url} target="_top" variant="primary">Grant permissions</Button>
        </div>
      </Banner>
    </div>
  );
}

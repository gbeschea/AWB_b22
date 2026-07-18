import { Routes, Route, Navigate, Link } from "react-router-dom";
import { NavMenu } from "@shopify/app-bridge-react";

import Home from "./pages/Home";
import Orders from "./pages/Orders";
import Validation from "./pages/Validation";
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
    <>
      <NavMenu>
        <Link to="/app" rel="home">
          Home
        </Link>
        <Link to="/app/orders">Orders</Link>
        <Link to="/app/validation">Address validation</Link>
        <Link to="/app/printing">Printing</Link>
        <Link to="/app/settings">Settings</Link>
        <Link to="/app/billing">Plans</Link>
      </NavMenu>

      <Routes>
        <Route path="/app" element={<Home />} />
        <Route path="/app/orders" element={<Orders />} />
        <Route path="/app/validation" element={<Validation />} />
        <Route path="/app/printing" element={<Printing />} />
        <Route path="/app/settings" element={<Settings />} />
        <Route path="/app/billing" element={<Billing />} />
        {/* Any unknown path (e.g. a bare "/") lands on Home. */}
        <Route path="*" element={<Navigate to="/app" replace />} />
      </Routes>
    </>
  );
}

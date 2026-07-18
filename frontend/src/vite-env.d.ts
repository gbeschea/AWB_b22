/// <reference types="vite/client" />

/**
 * App Bridge v4 is loaded from the CDN <script> in index.html and exposes a global
 * `shopify` object on window. We only use `idToken()` here; keep the surface minimal.
 */
interface ShopifyGlobal {
  idToken: () => Promise<string>;
  [key: string]: unknown;
}

declare global {
  interface Window {
    shopify: ShopifyGlobal;
  }
  // Available as a bare global inside the embedded app (App Bridge sets it up).
  const shopify: ShopifyGlobal;
}

export {};

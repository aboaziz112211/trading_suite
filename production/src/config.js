import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));

/**
 * Load the active site config. Switch site with the SITE env var.
 * e.g. SITE=roasting_house  → sites/roasting_house.json
 *      SITE=chartedge        → sites/chartedge.json  (default)
 */
export function loadSiteConfig() {
  const site = (process.env.SITE || "chartedge").replace(/[^a-z0-9_-]/gi, "");
  const path = join(__dirname, "..", "sites", `${site}.json`);
  return JSON.parse(readFileSync(path, "utf-8"));
}

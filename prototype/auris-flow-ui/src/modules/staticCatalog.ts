import { loadJsonCatalog } from "./catalogLoader";

const staticCatalogUrl = new URL("../catalogs/production/static-catalog.json", import.meta.url);
const staticCatalog = await loadJsonCatalog(staticCatalogUrl, "静态 catalog");

export default staticCatalog;

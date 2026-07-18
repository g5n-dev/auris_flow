import { loadJsonCatalog } from "./catalogLoader";

const moduleCatalogUrl = new URL("../catalogs/production/module-catalog.json", import.meta.url);
const moduleCatalog = await loadJsonCatalog(moduleCatalogUrl, "模块 catalog");

export default moduleCatalog;

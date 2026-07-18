import moduleCatalog from "../../modules/moduleCatalog";
import staticCatalog from "../../modules/staticCatalog";
import type { ModuleConfig } from "../../shared/contracts/modules";
import type { AssetApiContract, AssetCatalogRow, AssetCompatibilityCheck } from "./types";

const catalog = staticCatalog as {
  runtimeCatalog: {
    assetRows: AssetCatalogRow[];
    assetApiContracts: AssetApiContract[];
    assetDagsterCompatibilityChecks: AssetCompatibilityCheck[];
  };
};

const modules = moduleCatalog as {
  moduleConfigs: { assets: ModuleConfig };
};

export const assetRows = catalog.runtimeCatalog.assetRows;
export const assetApiContracts = catalog.runtimeCatalog.assetApiContracts;
export const assetDagsterCompatibilityChecks = catalog.runtimeCatalog.assetDagsterCompatibilityChecks;
export const assetModuleConfig = modules.moduleConfigs.assets;

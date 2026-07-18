import staticCatalog from "../../modules/staticCatalog";

export type DataAssetCatalogRow = {
  name: string;
  quality: number;
  assetKey: string;
};

const staticCatalogData = staticCatalog as {
  runtimeCatalog: {
    assetRows: DataAssetCatalogRow[];
  };
};

export const dataAssetCatalogRows = staticCatalogData.runtimeCatalog.assetRows;

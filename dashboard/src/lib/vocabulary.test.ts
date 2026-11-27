import { describe, expect, it } from "vitest"

import { SOURCE_PROVIDER_DATASETS } from "./generated/provider-catalog"
import {
  assetClassLabel,
  DATASET_NAMES,
  datasetLabel,
  withName,
} from "./vocabulary"

describe("display names", () => {
  it("shows the name with the key", () => {
    expect(assetClassLabel("index")).toBe("指數(index)")
    expect(datasetLabel("tw_equity_eod")).toBe("台股日K(tw_equity_eod)")
  })

  it("shows a key without a name as is", () => {
    expect(assetClassLabel("warrant")).toBe("warrant")
    expect(datasetLabel("new_dataset")).toBe("new_dataset")
    expect(withName({}, "toString")).toBe("toString")
  })

  it("names every dataset in the provider catalog", () => {
    const unnamed = Object.values(SOURCE_PROVIDER_DATASETS)
      .flat()
      .filter(key => !Object.hasOwn(DATASET_NAMES, key))
    expect(unnamed).toEqual([])
  })
})

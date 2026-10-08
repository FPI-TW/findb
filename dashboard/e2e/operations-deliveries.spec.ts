import { expect, test, type Page } from "@playwright/test"
const backend = "http://127.0.0.1:18081"
const plansPath = "/api/v1/admin/delivery-plans"
async function login(page: Page, role = "owner") {
  await page.goto("/dashboard/login")
  await page.waitForLoadState("networkidle")
  await page.getByLabel("帳號", { exact: true }).fill(role)
  await page.getByLabel("密碼", { exact: true }).fill("fixture-password")
  await page.getByRole("button", { name: "登入", exact: true }).click()
  await page.getByRole("link", { name: "交付監控", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "展開詳細資料" }).first()
  ).toBeVisible()
}
test.beforeEach(async ({ request }) => {
  await request.post(`${backend}/fixture/reset`, { data: {} })
})

test("tabs, filters, pagination, browser history and backfill draft remain independent", async ({
  page,
  request,
}) => {
  await login(page)
  await expect(page.getByRole("tab", { name: "全市場計畫" })).toHaveAttribute(
    "aria-selected",
    "true"
  )
  await page.getByRole("button", { name: "全市場計畫分頁下一頁" }).click()
  await expect(page).toHaveURL(/pp=2/)
  await page
    .getByRole("combobox", { name: "資料集", exact: true })
    .selectOption("old_dataset")
  await expect(page).toHaveURL(/dataset=old_dataset/)
  await expect(
    page.getByRole("table", { name: "全市場交付計畫" })
  ).toContainText("old_dataset")
  await page.reload()
  await expect(
    page.getByRole("combobox", { name: "資料集", exact: true })
  ).toHaveValue("old_dataset")
  await page.getByRole("tab", { name: "缺漏告警" }).click()
  await expect(page).toHaveURL(/tab=alerts/)
  await page
    .getByRole("button", { name: "帶入 tw_equity_minute 2026-10-07 回補參數" })
    .click()
  await expect(page.getByLabel("回補起始日期")).toHaveValue("2026-10-07")
  await page.getByLabel("回補結束日期").fill("2026-10-08")
  await page.getByRole("tab", { name: "全市場計畫" }).click()
  await expect(
    page.getByRole("combobox", { name: "資料集", exact: true })
  ).toHaveValue("old_dataset")
  await page.goBack()
  await expect(page.getByRole("tab", { name: "歷史回補" })).toHaveAttribute(
    "aria-selected",
    "true"
  )
  await expect(page.getByLabel("回補結束日期")).toHaveValue("2026-10-08")
  await expect(
    page.getByRole("button", { name: "建立回補", exact: true })
  ).toBeDisabled()
  await page.goForward()
  await expect(page.getByRole("tab", { name: "全市場計畫" })).toHaveAttribute(
    "aria-selected",
    "true"
  )
  expect(
    (await (await request.get(`${backend}/fixture/state`)).json()).mutations
  ).toEqual([])
})

test("three polling cycles and manual refresh preserve layout, focus, expansion and scroll; failures retain data", async ({
  page,
  request,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.clock.install()
  await login(page)
  await page.getByRole("button", { name: "展開詳細資料" }).first().click()
  const table = page.getByRole("table", { name: "全市場交付計畫" })
  const viewport = table.locator("..")
  const focus = page.getByLabel("交易日", { exact: true })
  await focus.focus()
  await viewport.evaluate(element => {
    element.scrollTop = 30
    element.scrollLeft = 60
  })
  const before = await table.boundingBox()
  const toolbarBefore = await focus.boundingBox()
  const scroll = await viewport.evaluate(element => [
    element.scrollTop,
    element.scrollLeft,
  ])
  await request.post(`${backend}/fixture/delivery-config`, {
    data: { delay: 400 },
  })
  for (let index = 0; index < 3; index++) {
    const count = (await (await request.get(`${backend}/fixture/state`)).json())
      .reads[plansPath]
    await page.clock.fastForward(60_000)
    await expect
      .poll(
        async () =>
          (await (await request.get(`${backend}/fixture/state`)).json()).reads[
            plansPath
          ]
      )
      .toBe(count + 1)
    await expect(
      page.getByText("正在更新全市場交付計畫…", { exact: false })
    ).toBeVisible()
    const during = await table.boundingBox()
    expect(Math.abs(during!.y - before!.y)).toBeLessThanOrEqual(1)
    expect(
      Math.abs((await focus.boundingBox())!.y - toolbarBefore!.y)
    ).toBeLessThanOrEqual(1)
    await expect(
      page.getByText("正在更新全市場交付計畫…", { exact: false })
    ).toHaveCount(0)
    await expect(focus).toBeFocused()
    await expect(
      page.getByRole("button", { name: "收合詳細資料" })
    ).toBeVisible()
    expect(
      await viewport.evaluate(element => [
        element.scrollTop,
        element.scrollLeft,
      ])
    ).toEqual(scroll)
  }
  await page
    .getByRole("button", { name: "重新整理", exact: true })
    .evaluate((button: HTMLButtonElement) => button.click())
  await expect(
    page.getByText("正在更新全市場交付計畫…", { exact: false })
  ).toBeVisible()
  expect(
    Math.abs((await table.boundingBox())!.y - before!.y)
  ).toBeLessThanOrEqual(1)
  await expect(
    page.getByText("正在更新全市場交付計畫…", { exact: false })
  ).toHaveCount(0)
  await request.post(`${backend}/fixture/delivery-config`, {
    data: { error: true },
  })
  await page.getByRole("button", { name: "重新整理", exact: true }).click()
  await expect(page.getByRole("alert")).toContainText("保留最後成功資料")
  await expect(page.getByRole("button", { name: "收合詳細資料" })).toBeVisible()
  await request.post(`${backend}/fixture/delivery-config`, { data: {} })
  await page.getByRole("button", { name: "重試", exact: true }).click()
  await expect(page.getByRole("alert")).toHaveCount(0)
  await page.screenshot({
    path: "/private/tmp/findb-deliveries-desktop.png",
    fullPage: true,
    animations: "disabled",
  })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.emulateMedia({ reducedMotion: "reduce", colorScheme: "dark" })
  await page.evaluate(() => document.documentElement.classList.add("dark"))
  await expect(table).toBeVisible()
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth
    )
  ).toBe(true)
  await page.screenshot({
    path: "/private/tmp/findb-deliveries-mobile.png",
    fullPage: true,
    animations: "disabled",
  })
})

test("viewer deep links cannot expose historical backfill and tab keyboard navigation works", async ({
  page,
  request,
}) => {
  await request.post(`${backend}/fixture/reset`, { data: { role: "viewer" } })
  await login(page, "viewer")
  await page.goto("/dashboard/operations/deliveries?tab=backfills")
  await expect(page.getByRole("tab", { name: "全市場計畫" })).toHaveAttribute(
    "aria-selected",
    "true"
  )
  await expect(page.getByRole("tab", { name: "歷史回補" })).toHaveCount(0)
  await page.getByRole("tab", { name: "全市場計畫" }).focus()
  await page.keyboard.press("ArrowRight")
  await expect(page.getByRole("tab", { name: "缺漏告警" })).toBeFocused()
  await expect(
    page.getByRole("table", { name: "未解決的交付缺漏" })
  ).toContainText("tw_equity_minute")
  await expect(
    page.getByRole("button", { name: /帶入.*回補參數/ })
  ).toHaveCount(0)
  const state = await (await request.get(`${backend}/fixture/state`)).json()
  expect(state.reads["/api/v1/admin/historical-backfills"]).toBeUndefined()
  expect(
    state.reads["/api/v1/admin/historical-backfills/scopes"]
  ).toBeUndefined()
})

for (const role of ["operator", "owner"]) {
  test(`${role} completes alert → preview → confirmed backfill exactly once while refreshes preserve its draft`, async ({
    page,
    request,
  }) => {
    await request.post(`${backend}/fixture/reset`, { data: { role } })
    await page.clock.install()
    await login(page, role)
    await page.getByRole("tab", { name: "缺漏告警" }).click()
    await page
      .getByRole("button", {
        name: "帶入 tw_equity_minute 2026-10-07 回補參數",
      })
      .click()
    await expect(page.locator("#backfill-provider")).toHaveValue("shioaji")
    await expect(page.locator("#backfill-dataset")).toHaveValue(
      "tw_equity_minute"
    )
    await expect(page.getByLabel("回補起始日期")).toHaveValue("2026-10-07")
    await expect(page.getByLabel("回補結束日期")).toHaveValue("2026-10-07")
    const create = page.getByRole("button", { name: "建立回補", exact: true })
    const confirmation = page.getByRole("checkbox")
    await expect(create).toBeDisabled()
    await expect(confirmation).toBeDisabled()
    expect(
      (await (await request.get(`${backend}/fixture/state`)).json()).mutations
    ).toEqual([])
    await page.getByRole("button", { name: "驗證交易日", exact: true }).click()
    const preview = page.getByText("範圍有效；所有日期都會建立工作項目。", {
      exact: true,
    })
    await expect(preview).toBeVisible()
    await expect(create).toBeDisabled()
    expect(
      (await (await request.get(`${backend}/fixture/state`)).json()).mutations
    ).toEqual([])
    await confirmation.check()
    await expect(create).toBeEnabled()
    await page.getByRole("tab", { name: "全市場計畫" }).click()
    await page.getByRole("button", { name: "重新整理", exact: true }).click()
    await page.getByRole("tab", { name: "歷史回補" }).click()
    await expect(preview).toBeVisible()
    await expect(confirmation).toBeChecked()
    await page.getByRole("button", { name: "重新整理", exact: true }).click()
    const count = (await (await request.get(`${backend}/fixture/state`)).json())
      .reads["/api/v1/admin/historical-backfills"]
    await page.clock.fastForward(60_000)
    await expect
      .poll(
        async () =>
          (await (await request.get(`${backend}/fixture/state`)).json()).reads[
            "/api/v1/admin/historical-backfills"
          ]
      )
      .toBeGreaterThan(count)
    await expect(page.locator("#backfill-provider")).toHaveValue("shioaji")
    await expect(page.locator("#backfill-dataset")).toHaveValue(
      "tw_equity_minute"
    )
    await expect(page.getByLabel("回補起始日期")).toHaveValue("2026-10-07")
    await expect(page.getByLabel("回補結束日期")).toHaveValue("2026-10-07")
    await expect(preview).toBeVisible()
    await expect(confirmation).toBeChecked()
    await expect(create).toBeEnabled()
    await create.click()
    await expect(
      page.locator("details").filter({ hasText: "shioaji/tw_equity_minute" })
    ).toContainText("2026-10-07 – 2026-10-07")
    await page.getByRole("button", { name: "重新整理", exact: true }).click()
    await expect(
      page.locator("details").filter({ hasText: "shioaji/tw_equity_minute" })
    ).toHaveCount(1)
    const state = await (await request.get(`${backend}/fixture/state`)).json()
    expect(state.previews).toEqual([
      {
        provider: "shioaji",
        dataset_key: "tw_equity_minute",
        start_date: "2026-10-07",
        end_date: "2026-10-07",
      },
    ])
    expect(state.mutations).toEqual([
      {
        path: "/api/v1/admin/historical-backfills",
        provider: "shioaji",
        dataset_key: "tw_equity_minute",
        start_date: "2026-10-07",
        end_date: "2026-10-07",
        request_key: expect.any(String),
      },
    ])
    expect(state.mutations[0].request_key).toMatch(/^[0-9a-f-]{36}$/)
  })
}

test("changed parameters and a new alert invalidate preview and confirmation; a late preview cannot authorize another draft", async ({
  page,
  request,
}) => {
  await login(page)
  await page.getByRole("tab", { name: "缺漏告警" }).click()
  await page
    .getByRole("button", { name: "帶入 tw_equity_minute 2026-10-07 回補參數" })
    .click()
  await page.getByRole("button", { name: "驗證交易日", exact: true }).click()
  await expect(page.getByRole("checkbox")).toBeEnabled()
  await page.getByRole("checkbox").check()
  await page.getByLabel("回補結束日期").fill("2026-10-08")
  await expect(page.getByRole("checkbox")).not.toBeChecked()
  await expect(page.getByRole("checkbox")).toBeDisabled()
  await expect(
    page.getByRole("button", { name: "建立回補", exact: true })
  ).toBeDisabled()
  await page.getByRole("button", { name: "驗證交易日", exact: true }).click()
  await expect(page.getByRole("checkbox")).toBeEnabled()
  await page.getByRole("checkbox").check()
  await page.getByRole("tab", { name: "缺漏告警" }).click()
  await page
    .getByRole("button", { name: "帶入 tw_equity_minute 2026-10-07 回補參數" })
    .click()
  await expect(page.getByLabel("回補結束日期")).toHaveValue("2026-10-07")
  await expect(page.getByRole("checkbox")).not.toBeChecked()
  await expect(page.getByRole("checkbox")).toBeDisabled()
  await expect(
    page.getByRole("button", { name: "建立回補", exact: true })
  ).toBeDisabled()
  await request.post(`${backend}/fixture/backfill-config`, {
    data: { previewDelay: 2000 },
  })
  const lateResponse = page.waitForResponse(
    async response =>
      response.url().includes("_serverFn") &&
      (await response.text()).includes("scope_valid")
  )
  await page.getByRole("button", { name: "驗證交易日", exact: true }).click()
  await expect(page.getByRole("button", { name: "驗證中…" })).toBeVisible()
  await page.getByRole("tab", { name: "缺漏告警" }).click()
  await page
    .getByRole("button", { name: "帶入 tw_equity_minute 2026-10-06 回補參數" })
    .click()
  await lateResponse
  await expect(page.getByLabel("回補起始日期")).toHaveValue("2026-10-06")
  await expect(page.getByLabel("回補結束日期")).toHaveValue("2026-10-06")
  await expect(page.getByRole("checkbox")).not.toBeChecked()
  await expect(page.getByRole("checkbox")).toBeDisabled()
  await expect(
    page.getByText("範圍有效；所有日期都會建立工作項目。", { exact: true })
  ).toHaveCount(0)
  await expect(
    page.getByRole("button", { name: "建立回補", exact: true })
  ).toBeDisabled()
  expect(
    (await (await request.get(`${backend}/fixture/state`)).json()).mutations
  ).toEqual([])
})

for (const expiry of ["local", "upstream"] as const) {
  test(`direct backfill preview ${expiry} session expiry removes the editor and returns to login`, async ({
    page,
    request,
  }) => {
    await login(page)
    await page.getByRole("tab", { name: "缺漏告警" }).click()
    await page
      .getByRole("button", {
        name: "帶入 tw_equity_minute 2026-10-07 回補參數",
      })
      .click()
    await expect(
      page.getByRole("button", { name: "驗證交易日", exact: true })
    ).toBeEnabled()
    await expect(
      page.locator("#historical-provider-scopes option")
    ).toHaveCount(1)
    await page.waitForLoadState("networkidle")
    await request.post(`${backend}/fixture/session`, {
      data:
        expiry === "local"
          ? { role: "viewer" }
          : { failurePaths: ["/api/v1/admin/historical-backfills/preview"] },
    })
    await page.getByRole("button", { name: "驗證交易日", exact: true }).click()
    await expect(page).toHaveURL(/\/login/)
    await expect(page.locator("#backfill-provider")).toHaveCount(0)
    await expect(
      page.locator("#historical-provider-scopes option")
    ).toHaveCount(0)
    await expect(
      page.getByRole("button", { name: "建立回補", exact: true })
    ).toHaveCount(0)
    expect(
      (await (await request.get(`${backend}/fixture/state`)).json()).mutations
    ).toEqual([])
  })
}

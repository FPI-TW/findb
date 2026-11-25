import {
  expect,
  test,
  type APIRequestContext,
  type Page,
} from "@playwright/test"
const backend = "http://127.0.0.1:18081"
const backfillsPath = "/api/v1/admin/historical-backfills"
async function reads(request: APIRequestContext, path: string) {
  const state = await (await request.get(`${backend}/fixture/state`)).json()
  return (state.reads[path] ?? 0) as number
}
async function login(page: Page, role = "owner") {
  await page.goto("/dashboard/login")
  await page.waitForLoadState("networkidle")
  await page.getByLabel("帳號", { exact: true }).fill(role)
  await page.getByLabel("密碼", { exact: true }).fill("fixture-password")
  await page.getByRole("button", { name: "登入", exact: true }).click()
  await page.getByRole("link", { name: "交付監控", exact: true }).click()
  await expect(
    page.getByRole("table", { name: "未解決的交付缺漏" })
  ).toContainText("tw_equity_eod")
}
// Hold the real tab module so SSR is observable before any tab handlers attach.
async function withDelayedTabs(
  page: Page,
  navigate: () => Promise<unknown>,
  checkServerContent: () => Promise<void>
) {
  const modulePath = "**/src/components/OperationsTabs.tsx*"
  let release!: () => void
  let requested!: () => void
  const gate = new Promise<void>(resolve => {
    release = resolve
  })
  const moduleRequested = new Promise<void>(resolve => {
    requested = resolve
  })
  const hydrationErrors: string[] = []
  page.on("console", message => {
    if (/hydration|hydrated|server rendered HTML/i.test(message.text())) {
      hydrationErrors.push(message.text())
    }
  })
  page.on("pageerror", error => hydrationErrors.push(error.message))
  await page.route(modulePath, async route => {
    const response = await route.fetch()
    requested()
    await gate
    await route.fulfill({ response })
  })
  try {
    await navigate()
    await moduleRequested
    await checkServerContent()
    const tabs = page.getByRole("tab")
    for (const tab of await tabs.all()) {
      await expect(tab).toBeDisabled()
      await expect(tab).toHaveAttribute("tabindex", "-1")
    }
  } finally {
    release()
  }
  for (const tab of await page.getByRole("tab").all()) {
    await expect(tab).toBeEnabled()
  }
  await page.unroute(modulePath)
  expect(hydrationErrors).toEqual([])
}

test.beforeEach(async ({ request }) => {
  await request.post(`${backend}/fixture/reset`, { data: {} })
})

test("authenticated provider queue shows active daily work and historical FIFO wait", async ({
  page,
  request,
}) => {
  await login(page)
  const queue = page.getByRole("region", { name: "供應商工作佇列" })
  await expect(
    queue.getByRole("tab", { name: "twelve_data (0)" })
  ).toHaveAttribute("aria-selected", "true")
  await expect(
    queue.getByText("此供應商目前沒有等待或執行中的工作。")
  ).toBeVisible()
  await queue.getByRole("tab", { name: "finlab (3)" }).click()
  await expect(
    queue.getByRole("table", { name: "finlab 工作佇列" })
  ).toBeVisible()
  await expect(
    queue.getByRole("cell", { name: "執行中 · running" })
  ).toBeVisible()
  await expect(queue.getByText("第 1 位", { exact: true })).toBeVisible()
  await expect(queue.getByText("等待市場收盤或重試時間")).toBeVisible()
  await expect(queue.getByText("等待同供應商目前工作完成")).toBeVisible()
  await expect(queue.getByText("僅排空原交付")).toBeVisible()
  await expect(queue.getByText("2099-10-19T01:00:00Z")).toBeVisible()
  await expect(queue.getByText("第 2 次執行", { exact: false })).toBeVisible()
  await expect(
    queue.getByText("2026-10-07T01:00:00Z", { exact: true })
  ).toHaveCount(3)
  await expect(queue.getByRole("alert")).toHaveCount(0)
  await queue.getByRole("tab", { name: "finlab (3)" }).press("ArrowRight")
  await expect(queue.getByRole("tab", { name: "fmp (0)" })).toHaveAttribute(
    "aria-selected",
    "true"
  )
  await expect(queue.getByRole("table")).toHaveCount(0)
  await expect(
    queue.getByText("此供應商目前沒有等待或執行中的工作。")
  ).toBeVisible()
  await queue.getByRole("tab", { name: "finlab (3)" }).click()
  await page.getByRole("tab", { name: "缺漏告警", exact: true }).click()
  await expect(queue.getByRole("tab", { name: "finlab (3)" })).toHaveAttribute(
    "aria-selected",
    "true"
  )
  const snapshot = await request.get(`${backend}/fixture/state`)
  expect(
    (await snapshot.json()).reads["/api/v1/admin/provider-work"]
  ).toBeGreaterThan(0)
})

test("tabs, browser history and backfill draft remain independent", async ({
  page,
  request,
}) => {
  await login(page)
  // Daily completeness has its own page; deliveries open on the alerts.
  await expect(page.getByRole("tab", { name: "交付計畫" })).toHaveCount(0)
  await expect(page.getByRole("tab", { name: "缺漏告警" })).toHaveAttribute(
    "aria-selected",
    "true"
  )
  await page.getByRole("tab", { name: "Universe 名單" }).click()
  await expect(page).toHaveURL(/tab=candidates/)
  await withDelayedTabs(
    page,
    () => page.reload({ waitUntil: "commit" }),
    async () => {
      await expect(
        page.getByRole("tab", { name: "Universe 名單" })
      ).toHaveAttribute("aria-selected", "true")
    }
  )
  await page.getByRole("tab", { name: "缺漏告警" }).click()
  await expect(page).toHaveURL(/tab=alerts/)
  await expect(page.getByRole("tab", { name: "缺漏告警" })).toBeFocused()
  await page
    .getByRole("button", { name: "帶入 tw_equity_eod 2026-10-07 回補參數" })
    .click()
  await expect(page.getByLabel("回補起始日期")).toHaveValue("2026-10-07")
  await page.getByLabel("回補結束日期").fill("2026-10-08")
  await page.getByRole("tab", { name: "Universe 名單" }).click()
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
  await expect(
    page.getByRole("tab", { name: "Universe 名單" })
  ).toHaveAttribute("aria-selected", "true")
  expect(
    (await (await request.get(`${backend}/fixture/state`)).json()).mutations
  ).toEqual([])
})

test("viewer deep links cannot expose historical backfill and tab keyboard navigation works", async ({
  page,
  request,
}) => {
  await request.post(`${backend}/fixture/reset`, { data: { role: "viewer" } })
  await login(page, "viewer")
  await withDelayedTabs(
    page,
    () =>
      page.goto("/dashboard/operations/deliveries?tab=backfills", {
        waitUntil: "commit",
      }),
    async () => {
      await expect(page.getByRole("tab", { name: "缺漏告警" })).toHaveAttribute(
        "aria-selected",
        "true"
      )
      await expect(page.getByRole("tab", { name: "歷史回補" })).toHaveCount(0)
    }
  )
  await page.getByRole("tab", { name: "缺漏告警" }).focus()
  await page.keyboard.press("ArrowRight")
  await expect(page.getByRole("tab", { name: "Universe 名單" })).toBeFocused()
  await expect(page).toHaveURL(/tab=candidates/)
  await page.keyboard.press("ArrowLeft")
  await expect(page.getByRole("tab", { name: "缺漏告警" })).toHaveAttribute(
    "aria-selected",
    "true"
  )
  await expect(
    page.getByRole("table", { name: "未解決的交付缺漏" })
  ).toContainText("tw_equity_eod")
  await expect(
    page.getByRole("button", { name: /帶入.*回補參數/ })
  ).toHaveCount(0)
  const state = await (await request.get(`${backend}/fixture/state`)).json()
  expect(state.mutations).toEqual([])
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
    await page
      .getByRole("button", {
        name: "帶入 tw_equity_eod 2026-10-07 回補參數",
      })
      .click()
    await expect(page.locator("#backfill-provider")).toHaveText("finlab")
    await expect(page.locator("#backfill-dataset")).toHaveText("tw_equity_eod")
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
    const preview = page.getByText("範圍有效；抓取與略過日期會在建立時凍結。", {
      exact: true,
    })
    await expect(preview).toBeVisible()
    await expect(create).toBeDisabled()
    expect(
      (await (await request.get(`${backend}/fixture/state`)).json()).mutations
    ).toEqual([])
    await confirmation.check()
    await expect(create).toBeEnabled()
    await page.getByRole("tab", { name: "缺漏告警" }).click()
    await page.getByRole("button", { name: "重新整理", exact: true }).click()
    await page.getByRole("tab", { name: "歷史回補" }).click()
    await expect(preview).toBeVisible()
    await expect(confirmation).toBeChecked()
    const refresh = page.getByRole("button", { name: "重新整理", exact: true })
    await expect(refresh).toBeEnabled()
    const requested = await reads(request, backfillsPath)
    await refresh.click()
    // The live interval restarts when a fetch settles. Advancing the clock while this
    // refresh is in flight fires the interval into it, and the next poll is a minute away.
    await expect
      .poll(() => reads(request, backfillsPath))
      .toBeGreaterThan(requested)
    await expect(refresh).toBeEnabled()
    const count = await reads(request, backfillsPath)
    await page.clock.fastForward(60_000)
    await expect
      .poll(() => reads(request, backfillsPath))
      .toBeGreaterThan(count)
    await expect(page.locator("#backfill-provider")).toHaveText("finlab")
    await expect(page.locator("#backfill-dataset")).toHaveText("tw_equity_eod")
    await expect(page.getByLabel("回補起始日期")).toHaveValue("2026-10-07")
    await expect(page.getByLabel("回補結束日期")).toHaveValue("2026-10-07")
    await expect(preview).toBeVisible()
    await expect(confirmation).toBeChecked()
    await expect(create).toBeEnabled()
    await create.click()
    await expect(
      page
        .getByRole("table", { name: "回補請求紀錄" })
        .getByRole("row")
        .filter({ hasText: "finlab/tw_equity_eod" })
    ).toContainText("2026-10-07 – 2026-10-07")
    await page.getByRole("button", { name: "重新整理", exact: true }).click()
    await expect(
      page
        .getByRole("table", { name: "回補請求紀錄" })
        .getByRole("row")
        .filter({ hasText: "finlab/tw_equity_eod" })
    ).toHaveCount(1)
    const state = await (await request.get(`${backend}/fixture/state`)).json()
    expect(state.previews).toEqual([
      {
        provider: "finlab",
        dataset_key: "tw_equity_eod",
        start_date: "2026-10-07",
        end_date: "2026-10-07",
      },
    ])
    expect(state.mutations).toEqual([
      {
        path: "/api/v1/admin/historical-backfills",
        provider: "finlab",
        dataset_key: "tw_equity_eod",
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
    .getByRole("button", { name: "帶入 tw_equity_eod 2026-10-07 回補參數" })
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
    .getByRole("button", { name: "帶入 tw_equity_eod 2026-10-07 回補參數" })
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
    .getByRole("button", { name: "帶入 tw_equity_eod 2026-10-06 回補參數" })
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
        name: "帶入 tw_equity_eod 2026-10-07 回補參數",
      })
      .click()
    await expect(
      page.getByRole("button", { name: "驗證交易日", exact: true })
    ).toBeEnabled()
    await expect(
      page.locator('select[name="provider"] option[value]:not([value=""])')
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
      page.locator('select[name="provider"] option[value]:not([value=""])')
    ).toHaveCount(0)
    await expect(
      page.getByRole("button", { name: "建立回補", exact: true })
    ).toHaveCount(0)
    expect(
      (await (await request.get(`${backend}/fixture/state`)).json()).mutations
    ).toEqual([])
  })
}

test("owner refreshes a universe by hand after reading the approval; a viewer cannot", async ({
  page,
  request,
}) => {
  const refreshPath = "/api/v1/admin/universes/refresh"
  await request.post(`${backend}/fixture/reset`, { data: { role: "owner" } })
  await login(page)
  await page.getByRole("tab", { name: "Universe 名單" }).click()
  await expect(page).toHaveURL(/tab=candidates/)
  const table = page.getByRole("table", { name: "Universe 名單", exact: true })
  await expect(table).toContainText("尚未發布 baseline")
  const trigger = page.getByRole("button", { name: "手動更新 tw_etf_eod 名單" })
  await trigger.click()
  const dialog = page.getByRole("alertdialog")
  await expect(dialog).toContainText("手動更新等同核准")
  await expect(dialog).toContainText("會成為第一份 baseline")
  const confirm = dialog.getByRole("button", { name: "確認更新並核准" })
  await expect(confirm).toBeDisabled()
  // Escape leaves nothing behind.
  await page.keyboard.press("Escape")
  await expect(dialog).toHaveCount(0)
  let state = await (await request.get(`${backend}/fixture/state`)).json()
  expect(state.mutations).toEqual([])

  await trigger.click()
  await dialog.getByLabel("更新原因").fill("已核對交易所公告的新上市商品")
  await confirm.click()
  await expect(table).toContainText("排隊中")
  await expect(table).toContainText("E2E Owner")
  await expect(trigger).toBeDisabled()
  state = await (await request.get(`${backend}/fixture/state`)).json()
  expect(state.mutations).toEqual([
    {
      path: refreshPath,
      provider: "finlab",
      dataset_key: "tw_etf_eod",
      evidence_note: "已核對交易所公告的新上市商品",
    },
  ])

  await request.post(`${backend}/fixture/reset`, { data: { role: "viewer" } })
  await login(page, "viewer")
  await page.getByRole("tab", { name: "Universe 名單" }).click()
  await expect(table).toContainText("僅 owner 可更新")
  await expect(page.getByRole("button", { name: /手動更新/ })).toHaveCount(0)
  state = await (await request.get(`${backend}/fixture/state`)).json()
  expect(state.mutations).toEqual([])
})

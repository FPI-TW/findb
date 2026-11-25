import {
  expect,
  test,
  type APIRequestContext,
  type Page,
} from "@playwright/test"
const backend = "http://127.0.0.1:18081"
const matrixPath = "/api/v1/admin/completeness"
async function reads(request: APIRequestContext, path: string) {
  const state = await (await request.get(`${backend}/fixture/state`)).json()
  return (state.reads[path] ?? 0) as number
}
async function openMatrix(page: Page, role = "owner") {
  await page.goto("/dashboard/login")
  await page.waitForLoadState("networkidle")
  await page.getByLabel("帳號", { exact: true }).fill(role)
  await page.getByLabel("密碼", { exact: true }).fill("fixture-password")
  await page.getByRole("button", { name: "登入", exact: true }).click()
  await page.getByRole("link", { name: "資料完整性", exact: true }).click()
  await expect(
    page.getByRole("table", { name: "完整性矩陣", exact: true })
  ).toContainText("tw_futures_eod")
}
const cell = (page: Page, date: string) =>
  page.getByRole("button", {
    name: new RegExp(`^tw_futures_eod taifex ${date}：`),
  })

test.beforeEach(async ({ request }) => {
  await request.post(`${backend}/fixture/reset`, { data: {} })
})

test("three polling cycles and manual refresh keep the selected cell, focus, layout and scroll; failures retain data", async ({
  page,
  request,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.clock.install()
  await openMatrix(page)
  await cell(page, "2026-09-15").click()
  await expect(page).toHaveURL(/dataset=tw_futures_eod/)
  const details = page.getByRole("region", { name: "所選日期明細" })
  await expect(details).toContainText("2026-09-15")
  const table = page.getByRole("table", { name: "完整性矩陣", exact: true })
  const viewport = table.locator("..")
  const focus = page.getByLabel("起日", { exact: true })
  await focus.focus()
  await viewport.evaluate(element => {
    element.scrollLeft = 120
  })
  const before = await table.boundingBox()
  const scroll = await viewport.evaluate(element => element.scrollLeft)
  expect(scroll).toBeGreaterThan(0)
  // Each refresh stays in flight until released, so its layout is measured mid-update.
  await request.post(`${backend}/fixture/delivery-config`, {
    data: { hold: true },
  })
  for (let index = 0; index < 3; index++) {
    const count = await reads(request, matrixPath)
    await page.clock.fastForward(60_000)
    await expect.poll(() => reads(request, matrixPath)).toBe(count + 1)
    await expect(
      page.getByText("正在更新完整性矩陣…", { exact: false })
    ).toBeVisible()
    expect(
      Math.abs((await table.boundingBox())!.y - before!.y)
    ).toBeLessThanOrEqual(1)
    await request.post(`${backend}/fixture/delivery-release`)
    await expect(
      page.getByText("正在更新完整性矩陣…", { exact: false })
    ).toHaveCount(0)
    await expect(focus).toBeFocused()
    await expect(cell(page, "2026-09-15")).toHaveAttribute(
      "aria-pressed",
      "true"
    )
    await expect(details).toContainText("2026-09-15")
    expect(await viewport.evaluate(element => element.scrollLeft)).toBe(scroll)
  }
  await request.post(`${backend}/fixture/delivery-config`, {
    data: { error: true },
  })
  await page.getByRole("button", { name: "重新整理", exact: true }).click()
  await expect(page.getByRole("alert")).toContainText("保留最後成功資料")
  await expect(details).toContainText("2026-09-15")
  await request.post(`${backend}/fixture/delivery-config`, { data: {} })
  await page.getByRole("button", { name: "重試", exact: true }).click()
  await expect(page.getByRole("alert")).toHaveCount(0)
  await page.screenshot({
    path: "/private/tmp/findb-completeness-desktop.png",
    fullPage: true,
    animations: "disabled",
  })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.emulateMedia({ reducedMotion: "reduce", colorScheme: "dark" })
  await page.evaluate(() => document.documentElement.classList.add("dark"))
  await expect(table).toBeVisible()
  // The matrix scrolls inside its own frame; the page never scrolls sideways.
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth
    )
  ).toBe(true)
  await page.screenshot({
    path: "/private/tmp/findb-completeness-mobile.png",
    fullPage: true,
    animations: "disabled",
  })
})

test("gap details load only when asked, filter by status, and carry a backfill draft", async ({
  page,
  request,
}) => {
  await openMatrix(page)
  await cell(page, "2026-09-30").click()
  const details = page.getByRole("region", { name: "所選日期明細" })
  await expect(details).toContainText("有未關閉告警")
  const summaryReads = async () => {
    const state = await (await request.get(`${backend}/fixture/state`)).json()
    return Object.entries(state.reads as Record<string, number>)
      .filter(([path]) => path.endsWith("/summary"))
      .reduce((sum, [, count]) => sum + count, 0)
  }
  expect(await summaryReads()).toBe(0)
  await details.getByRole("button", { name: "載入缺口明細" }).click()
  await expect(details).toContainText("TX")
  await expect(details).toContainText("MTX")
  await details
    .locator("#gap-status-0195a000-0000-7000-8000-000000000000")
    .click()
  await page.getByRole("option", { name: "受阻", exact: true }).click()
  await expect(details).toContainText("共 1 筆")
  await expect(details.getByText("TX", { exact: true })).toHaveCount(0)
  await details.getByRole("link", { name: "帶入回補" }).click()
  await expect(page.getByRole("tab", { name: "歷史回補" })).toHaveAttribute(
    "aria-selected",
    "true"
  )
  await expect(page.getByLabel("回補起始日期")).toHaveValue("2026-09-30")
  await expect(page.getByLabel("回補結束日期")).toHaveValue("2026-09-30")
  // The draft leaves the URL, so reloading never refills it.
  await expect(page).not.toHaveURL(/seed=[^&]/)
  expect(
    (await (await request.get(`${backend}/fixture/state`)).json()).mutations
  ).toEqual([])
})

test("old plan-list and retired profile links land on the matrix without pilot data", async ({
  page,
  request,
}) => {
  await openMatrix(page)
  await page.goto("/dashboard/operations/deliveries?tab=plans&profile=pilot")
  await expect(page).toHaveURL(/\/operations\/completeness/)
  const table = page.getByRole("table", { name: "完整性矩陣", exact: true })
  await expect(table).toContainText("tw_futures_eod")
  await expect(table).not.toContainText("pilot_dataset")
  await expect(page.getByText(/Pilot/)).toHaveCount(0)
  expect(
    (await (await request.get(`${backend}/fixture/state`)).json()).mutations
  ).toEqual([])
})

test("a viewer reads the matrix but is offered no backfill", async ({
  page,
  request,
}) => {
  await request.post(`${backend}/fixture/reset`, { data: { role: "viewer" } })
  await openMatrix(page, "viewer")
  await cell(page, "2026-09-30").click()
  const details = page.getByRole("region", { name: "所選日期明細" })
  await expect(details).toContainText("逾時")
  await expect(details.getByRole("link", { name: "帶入回補" })).toHaveCount(0)
  await expect(
    details.getByRole("link", { name: "查看缺漏告警" })
  ).toBeVisible()
})

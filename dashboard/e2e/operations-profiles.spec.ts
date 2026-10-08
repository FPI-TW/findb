import {
  expect,
  test,
  type APIRequestContext,
  type Page,
} from "@playwright/test"

const backend = "http://127.0.0.1:18081"

async function state(request: APIRequestContext) {
  return (await request.get(`${backend}/fixture/state`)).json()
}

async function login(page: Page, role = "owner") {
  await page.goto("/dashboard/login")
  await page.waitForLoadState("networkidle")
  await page.getByLabel("帳號", { exact: true }).fill(role)
  await page.getByLabel("密碼", { exact: true }).fill("fixture-password")
  await page.getByRole("button", { name: "登入", exact: true }).click()
  await expect(
    page.getByRole("tab", { name: "Pilot（1 執行中）" })
  ).toBeVisible()
}

test.beforeEach(async ({ request }) => {
  await request.post(`${backend}/fixture/reset`, { data: {} })
})

test("normal login, deep links and browser history select profiles without mutations", async ({
  page,
  request,
}) => {
  await page.goto("/dashboard/operations/?profile=full_market")
  await expect(page).toHaveURL(/\/login/)
  await login(page)
  await expect(
    page.getByRole("tab", { name: "Pilot（1 執行中）" })
  ).toHaveAttribute("aria-selected", "true")
  await expect(
    page.getByRole("region", { name: "Full market 排程" })
  ).toHaveCount(0)
  await page.getByRole("tab", { name: "Full market（1 執行中）" }).click()
  await expect(page).toHaveURL(/profile=full_market/)
  await expect(page.getByRole("region", { name: "Pilot 排程" })).toHaveCount(0)
  await expect(
    page.getByText("FULL_MARKET_ENABLED：true · production").first()
  ).toBeVisible()
  await expect(
    page.getByText("本次凍結範圍：tw_equity_eod").first()
  ).toBeVisible()
  await expect(
    page.getByText("tw_equity_eod：Ready · 首次日期 2026-10-06").first()
  ).toBeVisible()
  await expect(
    page.getByText("tw_etf_eod：baseline_missing · 首次日期 尚未啟動").first()
  ).toBeVisible()
  await expect(
    page.getByText(/部署前人工確認：請分別停止 Full market 與/)
  ).toBeVisible()
  await page.goBack()
  await expect(
    page.getByRole("tab", { name: "Pilot（1 執行中）" })
  ).toHaveAttribute("aria-selected", "true")
  await page.goForward()
  await expect(
    page.getByRole("tab", { name: "Full market（1 執行中）" })
  ).toHaveAttribute("aria-selected", "true")
  await page.reload()
  await expect(
    page.getByRole("region", { name: "Full market 排程" })
  ).toBeVisible()
  expect((await state(request)).mutations).toEqual([])
})

test("hidden counterpart warnings cancel safely and Pilot batch start/stop stays isolated", async ({
  page,
  request,
}) => {
  await login(page)
  await page
    .getByRole("button", { name: "Pilot finlab pilot_finlab 設為執行中" })
    .click()
  await expect(page.getByRole("alertdialog")).toContainText(
    "full_market_finlab_v1"
  )
  await page.getByRole("button", { name: "取消", exact: true }).click()
  expect((await state(request)).mutations).toEqual([])
  await page
    .getByRole("button", { name: "Pilot 全部啟動", exact: true })
    .click()
  await expect(page.getByRole("alertdialog")).toContainText(
    "Full market 與 Pilot 將同時執行"
  )
  await page.keyboard.press("Escape")
  await expect(page.getByRole("alertdialog")).toHaveCount(0)
  expect((await state(request)).mutations).toEqual([])
  await page
    .getByRole("button", { name: "Pilot 全部啟動", exact: true })
    .click()
  await page
    .getByRole("button", { name: "確認 Pilot 全部啟動", exact: true })
    .click()
  await expect(
    page.getByRole("tab", { name: "Pilot（2 執行中）" })
  ).toBeVisible()
  await page
    .getByRole("button", { name: "Pilot 全部停止", exact: true })
    .click()
  await page
    .getByRole("button", { name: "確認 Pilot 全部停止", exact: true })
    .click()
  await expect(
    page.getByRole("tab", { name: "Pilot（0 執行中）" })
  ).toBeVisible()
  expect((await state(request)).mutations).toEqual([
    {
      scheduler_key: "pilot_finlab",
      desired_state: "running",
      expected_revision: 1,
    },
    {
      scheduler_key: "pilot_finlab",
      desired_state: "stopped",
      expected_revision: 2,
    },
    {
      scheduler_key: "pilot_shioaji",
      desired_state: "stopped",
      expected_revision: 1,
    },
  ])
  await expect(
    page.getByRole("tab", { name: "Full market（1 執行中）" })
  ).toBeVisible()
})

test("Full market batch skips blocked and running controls, warns about hidden Pilot and stops only its profile", async ({
  page,
  request,
}) => {
  await login(page)
  await page.goto("/dashboard/operations/?profile=full_market")
  await expect(
    page.getByRole("note").filter({ hasText: "全部啟動將跳過" })
  ).toContainText("full_market_taifex_v1（缺少已發布的官方 baseline）")
  await page
    .getByRole("button", { name: "Full market 全部啟動", exact: true })
    .click()
  await expect(page.getByRole("alertdialog")).toContainText("pilot_shioaji")
  await page
    .getByRole("button", { name: "確認 Full market 全部啟動", exact: true })
    .click()
  await expect(
    page.getByRole("tab", { name: "Full market（2 執行中）" })
  ).toBeVisible()
  await page
    .getByRole("button", { name: "Full market 全部停止", exact: true })
    .click()
  await page
    .getByRole("button", { name: "確認 Full market 全部停止", exact: true })
    .click()
  await expect(
    page.getByRole("tab", { name: "Full market（0 執行中）" })
  ).toBeVisible()
  expect((await state(request)).mutations).toEqual([
    {
      scheduler_key: "full_market_shioaji_v1",
      desired_state: "running",
      expected_revision: 1,
    },
    {
      scheduler_key: "full_market_finlab_v1",
      desired_state: "stopped",
      expected_revision: 1,
    },
    {
      scheduler_key: "full_market_shioaji_v1",
      desired_state: "stopped",
      expected_revision: 2,
    },
  ])
  await expect(
    page.getByRole("tab", { name: "Pilot（1 執行中）" })
  ).toBeVisible()
})

test("all blocked starts and non-Owner sessions cannot mutate controls", async ({
  page,
  request,
}) => {
  await request.post(`${backend}/fixture/reset`, { data: { allBlocked: true } })
  await page.goto("/dashboard/login")
  await page.waitForLoadState("networkidle")
  await page.getByLabel("帳號", { exact: true }).fill("owner")
  await page.getByLabel("密碼", { exact: true }).fill("fixture-password")
  await page.getByRole("button", { name: "登入", exact: true }).click()
  await page.goto("/dashboard/operations/?profile=full_market")
  await expect(
    page.getByRole("button", { name: "Full market 全部啟動", exact: true })
  ).toBeDisabled()
  await expect(
    page.getByRole("note").filter({ hasText: "全部啟動將跳過" })
  ).toContainText("跳過 3 個 Scheduler")
  expect((await state(request)).mutations).toEqual([])
  await page.context().clearCookies()
  await request.post(`${backend}/fixture/reset`, { data: { role: "operator" } })
  await login(page, "operator")
  await expect(
    page.getByRole("button", { name: "Pilot 全部啟動", exact: true })
  ).toHaveCount(0)
  await page.getByRole("tab", { name: "Full market（1 執行中）" }).click()
  await expect(
    page.getByRole("button", { name: "Full market 全部停止", exact: true })
  ).toHaveCount(0)
  expect((await state(request)).mutations).toEqual([])
})

test("partial overview failure and recovery preserve controls, cards and queue positions", async ({
  page,
  request,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await login(page)
  const controls = page.getByRole("button", {
    name: "Pilot 全部啟動",
    exact: true,
  })
  const card = page.getByRole("button", {
    name: "Pilot finlab pilot_finlab 設為執行中",
  })
  const queue = page.getByRole("heading", {
    name: "Source ingest 後的佇列與 Worker",
    exact: true,
  })
  const targets = [controls, card, queue]
  const original = await Promise.all(
    targets.map(target => target.boundingBox())
  )
  await request.post(`${backend}/fixture/overview-config`, {
    data: { failurePath: "/api/v1/admin/schedulers" },
  })
  await page.getByRole("button", { name: "重新整理", exact: true }).click()
  await expect(page.getByRole("alert")).toContainText("保留最後成功資料")
  await expect(controls).toBeEnabled()
  for (const [index, target] of targets.entries())
    expect(
      Math.abs((await target.boundingBox())!.y - original[index]!.y)
    ).toBeLessThanOrEqual(1)
  await request.post(`${backend}/fixture/overview-config`, { data: {} })
  await page.getByRole("button", { name: "重試", exact: true }).click()
  await expect(page.getByRole("alert")).toHaveCount(0)
  for (const [index, target] of targets.entries())
    expect(
      Math.abs((await target.boundingBox())!.y - original[index]!.y)
    ).toBeLessThanOrEqual(1)
  expect((await state(request)).mutations).toEqual([])
})

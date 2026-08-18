import { expect, test } from "@playwright/test"

test("hydrates the canonical Lookup URL without an attribute mismatch", async ({
  page,
}) => {
  const hydrationErrors: string[] = []
  page.on("console", message => {
    if (
      message.type() === "error" &&
      (message.text().includes("hydrated but some attributes") ||
        message.text().includes("Hydration failed"))
    ) {
      hydrationErrors.push(message.text())
    }
  })

  await page.goto("/dashboard/lookup")
  await page.waitForTimeout(500)

  const headerHref = await page
    .getByRole("link", { name: "Instrument Lookup" })
    .getAttribute("href")
  const footerHref = await page
    .getByRole("navigation", { name: "頁尾導覽" })
    .getByRole("link", { name: "Lookup" })
    .getAttribute("href")

  expect(headerHref).toBe(footerHref)
  expect(headerHref).toContain("st=active")
  expect(hydrationErrors).toEqual([])
})

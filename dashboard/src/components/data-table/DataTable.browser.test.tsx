import type { ColumnDef } from "@tanstack/react-table"
import { afterEach, describe, expect, it } from "vitest"
import { cleanup, render } from "vitest-browser-react"

import "../../styles.css"
import { DataTable } from "./DataTable"

type BrowserRow = {
  id: string
  symbol: string
  market: string
  description: string
  status: string
}

const rows = Array.from({ length: 20 }, (_, index) => ({
  id: `row-${index}`,
  symbol: `SYMBOL-${index}`,
  market: index % 2 === 0 ? "TW" : "US",
  description: `A deliberately long description for responsive table verification ${index}`,
  status: index % 2 === 0 ? "active" : "paused",
}))

const columns: ColumnDef<BrowserRow, unknown>[] = [
  {
    accessorKey: "symbol",
    header: "Symbol",
    meta: { width: 180, pin: "left" },
  },
  {
    accessorKey: "market",
    header: "Market",
    meta: { minWidth: 140, pin: "left" },
  },
  {
    accessorKey: "description",
    header: "Description",
    meta: { width: 760, minWidth: 520, wrap: true },
  },
  {
    accessorKey: "status",
    header: "Status",
    meta: { width: 180 },
  },
  {
    id: "actions",
    header: "Actions",
    enableSorting: false,
    cell: () => <button type="button">Inspect</button>,
    meta: { width: 120, pin: "right", align: "right" },
  },
]

afterEach(() => {
  cleanup()
  document.documentElement.classList.remove("dark")
})

describe("DataTable browser layout", () => {
  it.each([375, 768, 1400])(
    "contains horizontal overflow at a %ipx surface without widening the page",
    async width => {
      const screen = await render(
        <div data-testid="surface" style={{ width }}>
          <DataTable
            ariaLabel="Responsive table"
            columns={columns}
            data={rows}
            getRowId={row => row.id}
          />
        </div>
      )
      const surface = screen.container.querySelector<HTMLElement>(
        '[data-testid="surface"]'
      )
      const viewport = screen.container.querySelector<HTMLElement>(
        '[data-slot="data-table-viewport"]'
      )

      expect(surface).not.toBeNull()
      expect(viewport).not.toBeNull()
      expect(viewport!.clientWidth).toBeLessThanOrEqual(width)
      if (width < 1400) expect(viewport!.scrollWidth).toBeGreaterThan(width)
      expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(
        document.documentElement.clientWidth
      )
    }
  )

  it("keeps headers and pinned edge columns sticky in the same viewport", async () => {
    const screen = await render(
      <div style={{ width: 768 }}>
        <DataTable
          ariaLabel="Pinned table"
          columns={columns}
          data={rows}
          getRowId={row => row.id}
        />
      </div>
    )
    const header = screen.container.querySelector<HTMLElement>(
      'th[data-column-id="symbol"]'
    )
    const leftCell = screen.container.querySelector<HTMLElement>(
      'td[data-column-id="symbol"]'
    )
    const secondLeftCell = screen.container.querySelector<HTMLElement>(
      'td[data-column-id="market"]'
    )
    const rightCell = screen.container.querySelector<HTMLElement>(
      'td[data-column-id="actions"]'
    )

    expect(getComputedStyle(header!).position).toBe("sticky")
    expect(getComputedStyle(header!).top).toBe("0px")
    expect(getComputedStyle(leftCell!).position).toBe("sticky")
    expect(getComputedStyle(leftCell!).left).toBe("0px")
    expect(getComputedStyle(secondLeftCell!).position).toBe("sticky")
    expect(getComputedStyle(secondLeftCell!).left).toBe("180px")
    expect(getComputedStyle(rightCell!).position).toBe("sticky")
    expect(getComputedStyle(rightCell!).right).toBe("0px")
  })

  it("fills a wide viewport when the columns are narrower", async () => {
    const screen = await render(
      <div data-testid="surface" style={{ width: 1200 }}>
        <DataTable
          ariaLabel="Full-width table"
          columns={columns.slice(0, 2)}
          data={rows}
          fillAvailableWidth
          getRowId={row => row.id}
        />
      </div>
    )
    const viewport = screen.container.querySelector<HTMLElement>(
      '[data-slot="data-table-viewport"]'
    )
    const table = screen.container.querySelector<HTMLTableElement>(
      '[data-slot="data-table-table"]'
    )

    expect(viewport).not.toBeNull()
    expect(table).not.toBeNull()
    expect(table!.getBoundingClientRect().width).toBe(viewport!.clientWidth)
  })

  it("distributes surplus width to data columns instead of the action column", async () => {
    const compactColumns: ColumnDef<BrowserRow, unknown>[] = [
      {
        accessorKey: "symbol",
        header: "Name",
        meta: { minWidth: 180 },
      },
      {
        accessorKey: "market",
        header: "Owner",
        meta: { width: 132 },
      },
      {
        accessorKey: "status",
        header: "Status",
        meta: { width: 112 },
      },
      {
        accessorKey: "description",
        header: "Description",
        meta: { minWidth: 300 },
      },
      {
        id: "actions",
        header: "Actions",
        meta: {
          width: 112,
          fitContent: true,
          pin: "right",
          align: "right",
        },
        cell: () => <button type="button">Inspect</button>,
      },
    ]
    const screen = await render(
      <div style={{ width: 1200 }}>
        <DataTable
          ariaLabel="Compact actions table"
          columns={compactColumns}
          data={rows}
          fillAvailableWidth
          getRowId={row => row.id}
          tableClassName="min-w-[980px]"
        />
      </div>
    )
    const viewport = screen.container.querySelector<HTMLElement>(
      '[data-slot="data-table-viewport"]'
    )
    const table = screen.container.querySelector<HTMLTableElement>(
      '[data-slot="data-table-table"]'
    )
    const nameCell = screen.container.querySelector<HTMLElement>(
      'td[data-column-id="symbol"]'
    )
    const actionsCell = screen.container.querySelector<HTMLElement>(
      'td[data-column-id="actions"]'
    )

    expect(viewport).not.toBeNull()
    expect(table).not.toBeNull()
    expect(nameCell).not.toBeNull()
    expect(actionsCell).not.toBeNull()
    expect(table!.getBoundingClientRect().width).toBe(viewport!.clientWidth)
    expect(actionsCell!.getBoundingClientRect().width).toBeCloseTo(112, 0)
    expect(nameCell!.getBoundingClientRect().width).toBeGreaterThan(180)
  })

  it("keeps sticky surfaces opaque in dark mode", async () => {
    document.documentElement.classList.add("dark")
    const screen = await render(
      <div style={{ width: 768 }}>
        <DataTable
          ariaLabel="Dark table"
          columns={columns}
          data={rows}
          getRowId={row => row.id}
        />
      </div>
    )
    const header = screen.container.querySelector<HTMLElement>(
      'th[data-column-id="symbol"]'
    )
    const pinnedCell = screen.container.querySelector<HTMLElement>(
      'td[data-column-id="symbol"]'
    )

    expect(getComputedStyle(header!).backgroundColor).not.toBe(
      "rgba(0, 0, 0, 0)"
    )
    expect(getComputedStyle(pinnedCell!).backgroundColor).not.toBe(
      "rgba(0, 0, 0, 0)"
    )
  })
})

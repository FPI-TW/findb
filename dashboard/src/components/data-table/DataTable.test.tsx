import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import type {
  ColumnDef,
  OnChangeFn,
  PaginationState,
} from "@tanstack/react-table"
import { afterEach, describe, expect, it, vi } from "vitest"

import { DataTable } from "./DataTable"

type Row = {
  id: string
  name: string
  status: string
}

const rows: Row[] = [
  { id: "row-1", name: "Alpha", status: "ready" },
  { id: "row-2", name: "Beta", status: "pending" },
]

const columns: ColumnDef<Row, unknown>[] = [
  {
    accessorKey: "name",
    header: "名稱",
    enableSorting: true,
    meta: { width: 120, pin: "left" },
  },
  {
    accessorKey: "status",
    header: "狀態",
    enableSorting: true,
    meta: { width: 120, align: "center", wrap: true, pin: "right" },
  },
]

describe("DataTable", () => {
  afterEach(() => {
    cleanup()
  })

  it("exposes the current sorting direction through aria-sort", () => {
    render(<DataTable caption="資料" columns={columns} data={rows} />)

    const nameHeader = screen.getByRole("columnheader", { name: /名稱/ })
    expect(nameHeader).toHaveAttribute("aria-sort", "none")

    fireEvent.click(screen.getByRole("button", { name: /依名稱排序/ }))
    expect(nameHeader).toHaveAttribute("aria-sort", "ascending")

    fireEvent.click(screen.getByRole("button", { name: /升冪/ }))
    expect(nameHeader).toHaveAttribute("aria-sort", "descending")
  })

  it("requires columns to opt in before exposing a sort control", () => {
    render(
      <DataTable
        caption="唯讀資料"
        columns={[
          {
            accessorKey: "name",
            header: "名稱",
          },
        ]}
        data={rows}
      />
    )

    expect(screen.queryByRole("button", { name: /排序/ })).toBeNull()
  })

  it("renders initial loading, error, and empty states", () => {
    const { rerender } = render(
      <DataTable caption="資料" columns={columns} data={[]} isLoading />
    )
    expect(screen.getByRole("status")).toHaveTextContent("正在載入資料")
    expect(screen.queryByText("目前沒有資料。")).not.toBeInTheDocument()

    rerender(
      <DataTable
        caption="資料"
        columns={columns}
        data={[]}
        error={new Error("request failed")}
        errorState="資料載入失敗"
      />
    )
    expect(screen.getByRole("alert")).toHaveTextContent("資料載入失敗")

    rerender(<DataTable caption="資料" columns={columns} data={[]} />)
    expect(screen.getByText("目前沒有資料。")).toBeInTheDocument()
  })

  it("uses deterministic sticky offsets for pinned columns", () => {
    render(<DataTable caption="資料" columns={columns} data={rows} />)

    const leftHeader = screen.getByRole("columnheader", { name: /名稱/ })
    const rightHeader = screen.getByRole("columnheader", { name: /狀態/ })
    expect(leftHeader).toHaveClass("sticky")
    expect(leftHeader.style.left).toBe("0px")
    expect(rightHeader).toHaveClass("sticky")
    expect(rightHeader.style.right).toBe("0px")
    expect(screen.getByRole("row", { name: /Alpha ready/ })).toHaveAttribute(
      "data-row-id",
      "row-1"
    )
  })

  it("toggles an expanded detail row with an accessible control", () => {
    render(
      <DataTable
        caption="資料"
        columns={columns}
        data={rows}
        renderExpandedRow={row => <span>Details for {row.original.id}</span>}
      />
    )

    const expandButton = screen.getAllByRole("button", {
      name: "展開詳細資料",
    })[0]
    if (!expandButton) throw new Error("Expected an expand button")
    fireEvent.click(expandButton)

    expect(screen.getByText("Details for row-1")).toBeInTheDocument()
    expect(expandButton).toHaveAttribute("aria-expanded", "true")
  })

  it("emits controlled pagination changes", () => {
    const onPaginationChange = vi.fn<OnChangeFn<PaginationState>>()
    render(
      <DataTable
        caption="資料"
        columns={columns}
        data={rows}
        manualPagination
        onPaginationChange={onPaginationChange}
        pageCount={3}
        pagination={{ pageIndex: 0, pageSize: 1 }}
      />
    )

    fireEvent.click(screen.getByRole("button", { name: "下一頁" }))
    expect(onPaginationChange).toHaveBeenCalledWith({
      pageIndex: 1,
      pageSize: 1,
    })
  })
})

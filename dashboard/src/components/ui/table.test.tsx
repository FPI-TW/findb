import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "./table"

describe("Table", () => {
  it("uses the page scrollbar in page mode", () => {
    render(
      <Table scrollMode="page">
        <TableHeader>
          <TableRow>
            <TableHead>Payload</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <TableRow>
            <TableCell>payload</TableCell>
          </TableRow>
        </TableBody>
      </Table>
    )

    const table = screen.getByRole("table")
    expect(table.classList.contains("w-max")).toBe(true)
    expect(table.classList.contains("min-w-full")).toBe(true)
    expect(table.parentElement).not.toBeNull()
    expect(table.parentElement?.classList.contains("max-h-80")).toBe(false)
    expect(table.parentElement?.classList.contains("overflow-auto")).toBe(false)
    expect(table.parentElement?.classList.contains("overflow-x-auto")).toBe(
      true
    )
    const header = screen.getByRole("columnheader")
    expect(header.classList.contains("top-0")).toBe(true)
    expect(header.classList.contains("top-16")).toBe(false)
  })
})

"use client"

import * as React from "react"

import { cn } from "#/lib/utils"

function Table({
  className,
  scrollMode = "container",
  ...props
}: React.ComponentProps<"table"> & {
  scrollMode?: "container" | "page"
}) {
  return (
    <div
      data-slot="table-container"
      className={
        scrollMode === "page"
          ? "relative min-w-full rounded-lg border border-line"
          : "relative max-h-80 w-full overflow-auto rounded-lg border border-line"
      }
    >
      <table
        data-slot="table"
        className={cn(
          scrollMode === "page" ? "w-max min-w-full" : "w-full",
          "caption-bottom text-xs",
          className
        )}
        {...props}
      />
    </div>
  )
}

function TableHeader({ className, ...props }: React.ComponentProps<"thead">) {
  return (
    <thead
      data-slot="table-header"
      className={cn("[&_tr]:border-b [&_tr]:border-line", className)}
      {...props}
    />
  )
}

function TableBody({ className, ...props }: React.ComponentProps<"tbody">) {
  return (
    <tbody
      data-slot="table-body"
      className={cn("[&_tr:last-child]:border-0", className)}
      {...props}
    />
  )
}

function TableFooter({ className, ...props }: React.ComponentProps<"tfoot">) {
  return (
    <tfoot
      data-slot="table-footer"
      className={cn(
        "border-t border-line bg-surface-soft font-medium [&>tr]:last:border-b-0",
        className
      )}
      {...props}
    />
  )
}

function TableRow({ className, ...props }: React.ComponentProps<"tr">) {
  return (
    <tr
      data-slot="table-row"
      className={cn(
        "border-b border-line transition-colors hover:bg-surface-soft has-aria-expanded:bg-surface-soft data-[state=selected]:bg-surface-soft",
        className
      )}
      {...props}
    />
  )
}

function TableHead({ className, ...props }: React.ComponentProps<"th">) {
  return (
    <th
      data-slot="table-head"
      className={cn(
        "sticky top-0 h-9 bg-surface-soft px-2.5 text-left align-middle text-xs font-bold tracking-wide whitespace-nowrap text-muted uppercase has-[[role=checkbox]]:pr-0 *:[[role=checkbox]]:translate-y-0.5",
        className
      )}
      {...props}
    />
  )
}

function TableCell({ className, ...props }: React.ComponentProps<"td">) {
  return (
    <td
      data-slot="table-cell"
      className={cn(
        "px-2.5 py-2 text-left align-top has-[[role=checkbox]]:pr-0 *:[[role=checkbox]]:translate-y-0.5",
        className
      )}
      {...props}
    />
  )
}

function TableCaption({
  className,
  ...props
}: React.ComponentProps<"caption">) {
  return (
    <caption
      data-slot="table-caption"
      className={cn("mt-4 text-sm text-muted", className)}
      {...props}
    />
  )
}

export {
  Table,
  TableHeader,
  TableBody,
  TableFooter,
  TableHead,
  TableRow,
  TableCell,
  TableCaption,
}

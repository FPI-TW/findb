import { Link } from "@tanstack/react-router"
import { Activity, Gauge, Search } from "lucide-react"

import { DEFAULT_SEARCH } from "../features/lookup/config"
import ThemeToggle from "./ThemeToggle"

export default function Header({ environment }: { environment: string }) {
  return (
    <header className="sticky top-0 z-20 border-b border-line/80 bg-page/90 backdrop-blur-lg">
      <div className="mx-auto flex min-h-16 w-full max-w-screen-2xl flex-wrap items-center justify-between gap-x-5 gap-y-2 px-3 py-2 sm:px-5">
        <div className="flex items-center gap-3">
          <Link
            to="/"
            activeOptions={{ exact: true }}
            className="flex items-center gap-2.5 rounded-lg font-extrabold text-ink no-underline outline-none focus-visible:ring-3 focus-visible:ring-accent/20"
          >
            <span className="inline-flex size-9 items-center justify-center rounded-xl bg-accent-soft text-accent">
              <Activity size={18} aria-hidden="true" />
            </span>
            <span>FinDB</span>
            <small className="hidden text-xs tracking-widest text-muted uppercase sm:inline">
              Data platform
            </small>
          </Link>
          <span
            aria-label={`當前環境：${environment}`}
            className="inline-flex min-h-8 items-center rounded-full bg-accent px-3 font-mono text-xs font-black tracking-wider text-white shadow-sm ring-2 ring-accent/25"
          >
            {environment}
          </span>
        </div>
        <div className="flex items-center gap-1 sm:gap-2">
          <nav className="flex items-center" aria-label="主要導覽">
            <Link
              to="/lookup"
              search={DEFAULT_SEARCH}
              aria-label="Instrument Lookup"
              activeProps={{ className: "bg-accent-soft text-accent" }}
              className="inline-flex min-h-9 items-center gap-1.5 rounded-lg px-2.5 text-sm font-bold text-muted transition-colors hover:bg-surface-soft hover:text-ink focus-visible:ring-3 focus-visible:ring-accent/20 focus-visible:outline-none"
            >
              <Search className="size-4" aria-hidden="true" />
              <span className="hidden sm:inline">Lookup</span>
            </Link>
            <Link
              to="/operations"
              aria-label="Operations 營運台"
              activeProps={{ className: "bg-accent-soft text-accent" }}
              className="inline-flex min-h-9 items-center gap-1.5 rounded-lg px-2.5 text-sm font-bold text-muted transition-colors hover:bg-surface-soft hover:text-ink focus-visible:ring-3 focus-visible:ring-accent/20 focus-visible:outline-none"
            >
              <Gauge className="size-4" aria-hidden="true" />
              <span className="hidden sm:inline">Operations</span>
            </Link>
          </nav>
          <ThemeToggle />
        </div>
      </div>
    </header>
  )
}

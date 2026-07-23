import { Link } from "@tanstack/react-router"

import { DEFAULT_SEARCH } from "../features/lookup/config"
import { Separator } from "./ui/separator"

export default function Footer() {
  return (
    <footer className="mx-auto w-full max-w-screen-2xl px-3 pb-9 text-xs text-muted sm:px-5">
      <Separator />
      <div className="flex flex-col justify-between gap-6 pt-6 sm:flex-row sm:items-center">
        <div className="flex flex-wrap gap-x-3 gap-y-1">
          <strong className="text-ink">FinDB Data Platform</strong>
          <span>Fetch → Normalize → Serve</span>
        </div>
        <nav className="flex flex-wrap gap-x-4 gap-y-2" aria-label="頁尾導覽">
          <Link className="font-bold text-ink hover:text-accent" to="/">
            首頁
          </Link>
          <Link
            className="font-bold text-ink hover:text-accent"
            to="/lookup"
            search={DEFAULT_SEARCH}
          >
            Lookup
          </Link>
          <Link className="font-bold text-ink hover:text-accent" to="/skill">
            Skill
          </Link>
          <Link
            className="font-bold text-ink hover:text-accent"
            to="/operations"
          >
            Operations
          </Link>
        </nav>
      </div>
    </footer>
  )
}

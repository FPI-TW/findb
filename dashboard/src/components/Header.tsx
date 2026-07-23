import { Link } from "@tanstack/react-router"
import { Activity } from "lucide-react"

import ThemeToggle from "./ThemeToggle"
import { Badge } from "./ui/badge"

export default function Header() {
  return (
    <header className="sticky top-0 z-20 border-b border-line/80 bg-page/90 backdrop-blur-lg">
      <div className="mx-auto flex min-h-16 w-full max-w-screen-2xl items-center justify-between gap-5 px-3 sm:px-5">
        <Link
          to="/"
          className="flex items-center gap-2.5 font-extrabold text-ink no-underline"
        >
          <span className="inline-flex size-9 items-center justify-center rounded-xl bg-accent-soft text-accent">
            <Activity size={18} />
          </span>
          <span>FinDB</span>
          <small className="hidden text-xs tracking-widest text-muted uppercase sm:inline">
            Operations Console
          </small>
        </Link>
        <div className="flex items-center gap-2.5">
          <Badge className="hidden sm:inline-flex" variant="outline">
            唯讀模式
          </Badge>
          <ThemeToggle />
        </div>
      </div>
    </header>
  )
}

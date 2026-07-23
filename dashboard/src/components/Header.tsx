import { Link } from "@tanstack/react-router"
import { Activity } from "lucide-react"

import ThemeToggle from "./ThemeToggle"

export default function Header() {
  return (
    <header className="app-header">
      <div className="header-inner">
        <Link to="/" className="brand">
          <span className="brand-mark">
            <Activity size={18} />
          </span>
          <span>FinDB</span>
          <small>Operations Console</small>
        </Link>
        <div className="header-meta">
          <span className="read-only-badge">唯讀模式</span>
          <ThemeToggle />
        </div>
      </div>
    </header>
  )
}

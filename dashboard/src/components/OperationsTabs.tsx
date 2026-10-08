import { useHydrated } from "@tanstack/react-router"
import { useId, type ReactNode } from "react"
import { Button } from "./ui/button"

export function OperationsTabs<T extends string>({
  label,
  tabs,
  selected,
  onChange,
  children,
}: {
  label: string
  tabs: readonly { value: T; label: string }[]
  selected: T
  onChange: (value: T) => void
  children: (value: T) => ReactNode
}) {
  const id = useId()
  const hydrated = useHydrated()
  return (
    <>
      <div
        role="tablist"
        aria-label={label}
        className="mb-4 flex gap-2 overflow-x-auto"
      >
        {tabs.map((tab, index) => (
          <Button
            key={tab.value}
            role="tab"
            type="button"
            id={`${id}-tab-${tab.value}`}
            aria-controls={`${id}-panel-${tab.value}`}
            aria-selected={selected === tab.value}
            disabled={!hydrated}
            tabIndex={hydrated && selected === tab.value ? 0 : -1}
            variant={selected === tab.value ? "default" : "outline"}
            onClick={() => onChange(tab.value)}
            onKeyDown={event => {
              const next =
                event.key === "ArrowRight"
                  ? (index + 1) % tabs.length
                  : event.key === "ArrowLeft"
                    ? (index + tabs.length - 1) % tabs.length
                    : event.key === "Home"
                      ? 0
                      : event.key === "End"
                        ? tabs.length - 1
                        : null
              if (next === null) return
              event.preventDefault()
              const value = tabs[next]!.value
              document.getElementById(`${id}-tab-${value}`)?.focus()
              onChange(value)
            }}
          >
            {tab.label}
          </Button>
        ))}
      </div>
      {tabs.map(tab => (
        <div
          key={tab.value}
          role="tabpanel"
          id={`${id}-panel-${tab.value}`}
          aria-labelledby={`${id}-tab-${tab.value}`}
          hidden={selected !== tab.value}
        >
          {children(tab.value)}
        </div>
      ))}
    </>
  )
}

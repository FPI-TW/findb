import { CheckCircle2, CircleAlert, Info, X } from "lucide-react"
import { useSyncExternalStore } from "react"

import { Button } from "./button"

type ToastVariant = "success" | "error" | "info"

type ToastItem = {
  id: number
  title: string
  description?: string
  variant: ToastVariant
}

type ToastOptions = {
  description?: string
  duration?: number
}

const EMPTY_TOASTS: ToastItem[] = []
let nextId = 0
let toasts: ToastItem[] = []
const listeners = new Set<() => void>()
const timers = new Map<number, ReturnType<typeof setTimeout>>()

function emit() {
  listeners.forEach(listener => listener())
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

function dismiss(id?: number) {
  if (id === undefined) {
    timers.forEach(timer => clearTimeout(timer))
    timers.clear()
    toasts = []
    emit()
    return
  }

  const timer = timers.get(id)
  if (timer) clearTimeout(timer)
  timers.delete(id)
  toasts = toasts.filter(item => item.id !== id)
  emit()
}

function show(
  variant: ToastVariant,
  title: string,
  { description, duration = 5000 }: ToastOptions = {}
) {
  const id = ++nextId
  const item: ToastItem = {
    id,
    title,
    variant,
    ...(description ? { description } : {}),
  }
  toasts = [...toasts, item].slice(-4)
  emit()

  if (duration > 0) {
    timers.set(
      id,
      setTimeout(() => dismiss(id), duration)
    )
  }

  return id
}

export const toast = {
  success: (title: string, options?: ToastOptions) =>
    show("success", title, options),
  error: (title: string, options?: ToastOptions) =>
    show("error", title, options),
  info: (title: string, options?: ToastOptions) => show("info", title, options),
  dismiss,
}

function ToastIcon({ variant }: { variant: ToastVariant }) {
  if (variant === "success") {
    return <CheckCircle2 className="mt-0.5 text-accent" aria-hidden="true" />
  }
  if (variant === "error") {
    return <CircleAlert className="mt-0.5 text-danger" aria-hidden="true" />
  }
  return <Info className="mt-0.5 text-muted" aria-hidden="true" />
}

export function Toaster() {
  const items = useSyncExternalStore(
    subscribe,
    () => toasts,
    () => EMPTY_TOASTS
  )

  return (
    <div
      className="pointer-events-none fixed right-4 bottom-4 z-[60] flex w-[calc(100%-2rem)] max-w-sm flex-col gap-2 sm:right-6 sm:bottom-6"
      aria-label="通知"
    >
      {items.map(item => (
        <div
          key={item.id}
          className="pointer-events-auto grid grid-cols-[auto_1fr_auto] gap-3 rounded-xl border border-line bg-surface p-4 text-ink shadow-lg"
          role={item.variant === "error" ? "alert" : "status"}
        >
          <ToastIcon variant={item.variant} />
          <div className="min-w-0">
            <p className="font-semibold">{item.title}</p>
            {item.description && (
              <p className="mt-1 text-sm leading-5 text-muted">
                {item.description}
              </p>
            )}
          </div>
          <Button
            className="-mt-1 -mr-1"
            type="button"
            size="icon-sm"
            variant="ghost"
            aria-label="關閉通知"
            onClick={() => dismiss(item.id)}
          >
            <X aria-hidden="true" />
          </Button>
        </div>
      ))}
    </div>
  )
}

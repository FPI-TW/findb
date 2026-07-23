import { Separator } from "./ui/separator"

export default function Footer() {
  return (
    <footer className="mx-auto w-[min(1480px,calc(100%-24px))] pb-9 text-xs text-muted sm:w-[min(1480px,calc(100%-40px))]">
      <Separator />
      <div className="flex flex-col justify-between gap-6 pt-6 sm:flex-row">
        <div className="flex gap-3">
          <strong className="text-ink">FinDB Operations Console</strong>
          <span>Fetch → Normalize → Serve</span>
        </div>
        <p className="m-0">此介面不提供資料修改或重跑操作。</p>
      </div>
    </footer>
  )
}

export default function Footer() {
  return (
    <footer className="mx-auto flex w-[min(1480px,calc(100%-24px))] flex-col justify-between gap-6 border-t border-line pt-6 pb-9 text-[0.72rem] text-muted sm:w-[min(1480px,calc(100%-40px))] sm:flex-row">
      <div className="flex gap-3">
        <strong className="text-ink">FinDB Operations Console</strong>
        <span>Fetch → Normalize → Serve</span>
      </div>
      <p className="m-0">此介面不提供資料修改或重跑操作。</p>
    </footer>
  )
}

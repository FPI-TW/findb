import { Check, Copy, KeyRound, X } from "lucide-react"
import { useState } from "react"

import { Alert, AlertDescription, AlertTitle } from "../../components/ui/alert"
import { Button } from "../../components/ui/button"
import { Card, CardContent, CardHeader } from "../../components/ui/card"

export function SecretDialog({
  title,
  secret,
  onClose,
}: {
  title: string
  secret: string
  onClose: () => void
}) {
  const [copied, setCopied] = useState(false)

  async function copy() {
    await navigator.clipboard.writeText(secret)
    setCopied(true)
  }

  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-black/55 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="secret-dialog-title"
    >
      <Card className="w-full max-w-xl shadow-xl">
        <CardHeader className="flex grid-cols-none flex-row items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <span className="inline-flex size-10 items-center justify-center rounded-xl bg-accent-soft text-accent">
              <KeyRound size={20} />
            </span>
            <h2 id="secret-dialog-title" className="text-xl font-bold">
              {title}
            </h2>
          </div>
          <Button
            type="button"
            size="icon"
            variant="ghost"
            aria-label="關閉"
            onClick={onClose}
          >
            <X size={18} />
          </Button>
        </CardHeader>
        <CardContent className="grid gap-4">
          <Alert variant="warning">
            <AlertTitle>此密鑰只會顯示一次</AlertTitle>
            <AlertDescription>
              關閉視窗前請先複製到安全的 secret storage；之後無法再次取得。
            </AlertDescription>
          </Alert>
          <code className="max-h-36 overflow-auto rounded-lg border border-line bg-surface-soft p-4 font-mono text-sm break-all select-all">
            {secret}
          </code>
          <Button type="button" onClick={() => void copy()}>
            {copied ? <Check size={17} /> : <Copy size={17} />}
            {copied ? "已複製" : "複製密鑰"}
          </Button>
        </CardContent>
      </Card>
    </div>
  )
}

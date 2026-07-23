import { Check, Copy, TriangleAlert } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { Button } from "../../components/ui/button"
import { copyTextToClipboard } from "./clipboard"

export default function CopyButton({
  text,
  label = "複製",
}: {
  text: string
  label?: string
}) {
  const [feedback, setFeedback] = useState<"idle" | "copied" | "failed">("idle")
  const resetTimer = useRef<number | null>(null)

  useEffect(
    () => () => {
      if (resetTimer.current !== null) window.clearTimeout(resetTimer.current)
    },
    []
  )

  async function copy() {
    const copied = await copyTextToClipboard(text)
    setFeedback(copied ? "copied" : "failed")

    if (resetTimer.current !== null) window.clearTimeout(resetTimer.current)
    resetTimer.current = window.setTimeout(() => setFeedback("idle"), 1800)
  }

  return (
    <div className="ml-auto inline-flex items-center">
      <Button
        type="button"
        size="sm"
        variant="ghost"
        onClick={() => void copy()}
        aria-label={label}
      >
        {feedback === "copied" ? (
          <Check aria-hidden="true" />
        ) : feedback === "failed" ? (
          <TriangleAlert aria-hidden="true" />
        ) : (
          <Copy aria-hidden="true" />
        )}
        <span className="hidden sm:inline">
          {feedback === "copied"
            ? "已複製"
            : feedback === "failed"
              ? "複製失敗"
              : "複製"}
        </span>
      </Button>
      <span className="sr-only" role="status" aria-live="polite">
        {feedback === "copied"
          ? "內容已複製到剪貼簿"
          : feedback === "failed"
            ? "無法自動複製，請手動選取內容"
            : ""}
      </span>
    </div>
  )
}

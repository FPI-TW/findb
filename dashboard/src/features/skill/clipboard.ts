export type OperatingSystem = "unix" | "windows"

export function detectOperatingSystem(
  userAgent: string,
  platform: string
): OperatingSystem {
  const identity = `${userAgent} ${platform}`.toLowerCase()
  return /windows|win32|win64/.test(identity) ? "windows" : "unix"
}

export async function copyTextToClipboard(
  text: string,
  clipboard: Pick<Clipboard, "writeText"> | undefined = typeof navigator ===
  "undefined"
    ? undefined
    : navigator.clipboard,
  currentDocument: Document | undefined = typeof document === "undefined"
    ? undefined
    : document
) {
  if (clipboard) {
    try {
      await clipboard.writeText(text)
      return true
    } catch {
      // Fall through for browsers and non-secure contexts without clipboard access.
    }
  }

  if (!currentDocument || typeof currentDocument.execCommand !== "function") {
    return false
  }

  const textArea = currentDocument.createElement("textarea")
  textArea.value = text
  textArea.setAttribute("readonly", "")
  textArea.style.position = "fixed"
  textArea.style.opacity = "0"
  currentDocument.body.appendChild(textArea)
  textArea.select()

  try {
    return currentDocument.execCommand("copy")
  } catch {
    return false
  } finally {
    textArea.remove()
  }
}

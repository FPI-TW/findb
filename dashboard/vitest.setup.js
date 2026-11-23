import "@testing-library/jest-dom/vitest"

// jsdom lacks the pointer-capture and scrolling APIs Radix Select calls while open.
if (typeof Element !== "undefined") {
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.releasePointerCapture ??= () => undefined
  Element.prototype.scrollIntoView ??= () => undefined
}

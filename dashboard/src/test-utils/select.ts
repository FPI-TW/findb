import { fireEvent, screen } from "@testing-library/react"

/** Open a shadcn/Radix Select from its trigger and return the listed option names. */
export function openSelect(trigger: HTMLElement): string[] {
  fireEvent.keyDown(trigger, { key: "Enter" })
  return screen
    .getAllByRole("option")
    .map(option => option.textContent?.trim() ?? "")
}

/** Close an open Select without changing its value. */
export function closeSelect() {
  fireEvent.keyDown(screen.getByRole("listbox"), { key: "Escape" })
}

/** Pick an option of a shadcn/Radix Select by its visible label. */
export function chooseSelectOption(trigger: HTMLElement, option: string) {
  fireEvent.keyDown(trigger, { key: "Enter" })
  fireEvent.click(screen.getByRole("option", { name: option }))
}

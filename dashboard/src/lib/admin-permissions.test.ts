import { describe, expect, it } from "vitest"

import {
  canIssueCredential,
  canManageCredential,
  canViewUsers,
  requiresPasswordChangeRedirect,
} from "./admin-permissions"

describe("Dashboard role and password-change guards", () => {
  it("shows user management only to owners", () => {
    expect(canViewUsers("owner")).toBe(true)
    expect(canViewUsers("operator")).toBe(false)
    expect(canViewUsers("viewer")).toBe(false)
  })

  it("limits credential mutations by role and kind", () => {
    expect(canIssueCredential("owner", "admin")).toBe(true)
    expect(canIssueCredential("operator", "source")).toBe(true)
    expect(canIssueCredential("operator", "serve")).toBe(true)
    expect(canIssueCredential("operator", "admin")).toBe(false)
    expect(canManageCredential("viewer", "serve")).toBe(false)
  })

  it("blocks normal routes until a temporary password is changed", () => {
    expect(requiresPasswordChangeRedirect(true, "/dashboard/operations")).toBe(
      true
    )
    expect(
      requiresPasswordChangeRedirect(true, "/dashboard/change-password")
    ).toBe(false)
    expect(requiresPasswordChangeRedirect(false, "/dashboard/operations")).toBe(
      false
    )
  })
})

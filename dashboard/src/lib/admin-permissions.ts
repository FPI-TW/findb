import type { AdminRole, CredentialKind } from "./admin-governance-api"

export function canViewUsers(role: AdminRole) {
  return role === "owner"
}

export function canIssueCredential(role: AdminRole, kind: CredentialKind) {
  return (
    role === "owner" ||
    (role === "operator" && (kind === "source" || kind === "serve"))
  )
}

export function canManageCredential(role: AdminRole, kind: CredentialKind) {
  return canIssueCredential(role, kind)
}

export function requiresPasswordChangeRedirect(
  mustChangePassword: boolean,
  pathname: string
) {
  return mustChangePassword && !pathname.endsWith("/change-password")
}

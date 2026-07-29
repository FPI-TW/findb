import { describe, expect, it } from "vitest"

import {
  adminLoginResponseSchema,
  createCredentialSchema,
  credentialMutationResponseSchema,
  credentialsOverviewSchema,
  credentialsResponseSchema,
  userMutationResponseSchema,
} from "./admin-governance-api"

const timestamp = "2026-07-28T02:00:00Z"
const user = {
  user_id: "019565d2-f838-7c91-85c1-72d4d7bbbe97",
  username: "owner",
  display_name: "Primary Owner",
  role: "owner",
  is_active: true,
  must_change_password: false,
}
const credential = {
  credential_ref: "serve:019565d2-f838-7c91-85c1-72d4d7bbbe98",
  id: "019565d2-f838-7c91-85c1-72d4d7bbbe98",
  kind: "serve",
  name: "lookup",
  owner: "web",
  description: null,
  status: "active",
  fingerprint: "sha256:abcd",
  role: null,
  scopes: ["eod:read"],
  policies: { page_size_limit: 100 },
  created_at: timestamp,
  expires_at: null,
  revoked_at: null,
  last_used_at: timestamp,
  usage_count: 42,
  rotated_from_id: null,
}

describe("admin governance wire contracts", () => {
  it("parses backend login and keeps opaque secrets out of the user object", () => {
    const result = adminLoginResponseSchema.parse({
      access_token: "opaque-token",
      token_type: "bearer",
      expires_at: timestamp,
      user,
    })
    expect(result.access_token).toBe("opaque-token")
    expect(result.user).not.toHaveProperty("access_token")
  })

  it("parses credential list and overview status", () => {
    expect(
      credentialsResponseSchema.parse({ data: [credential] }).data[0]
    ).toMatchObject({
      kind: "serve",
      usage_count: 42,
    })
    expect(
      credentialsOverviewSchema.parse({
        auth_mode: "db_with_legacy_fallback",
        serve_require_auth: true,
        legacy: { admin: false, source: true, serve: false },
        counts: {
          active: 1,
          expiring: 0,
          expired: 0,
          revoked: 0,
          legacy: 1,
        },
        usage_updated_at: timestamp,
      }).legacy.source
    ).toBe(true)
  })

  it("validates discriminated source, serve, and admin issue inputs", () => {
    expect(
      createCredentialSchema.parse({
        kind: "source",
        name: "fetcher",
        owner: "data-platform",
        source_name: "twelve_data",
        allowed_datasets: ["us_equity_eod"],
      }).kind
    ).toBe("source")
    expect(() =>
      createCredentialSchema.parse({
        kind: "source",
        name: "blocked-fetcher",
        owner: "data-platform",
        source_name: "twelve_data",
        allowed_datasets: [],
      })
    ).toThrow()
    expect(
      createCredentialSchema.parse({
        kind: "source",
        name: "provider-wide-fetcher",
        owner: "data-platform",
        source_name: "finlab",
        allowed_datasets: null,
      })
    ).toMatchObject({
      kind: "source",
      allowed_datasets: null,
    })
    expect(() =>
      createCredentialSchema.parse({
        kind: "source",
        name: "cross-provider",
        owner: "data-platform",
        source_name: "twelve_data",
        allowed_datasets: ["tw_equity_eod"],
      })
    ).toThrow()
    expect(
      createCredentialSchema.parse({
        kind: "serve",
        name: "lookup",
        owner: "web",
      }).kind
    ).toBe("serve")
    expect(
      createCredentialSchema.parse({
        kind: "admin",
        name: "automation",
        owner: "platform",
        role: "viewer",
      }).kind
    ).toBe("admin")
    expect(
      createCredentialSchema.safeParse({
        kind: "admin",
        owner: "",
        role: "root",
      }).success
    ).toBe(false)
  })

  it("parses one-time credential and temporary-password responses", () => {
    expect(
      credentialMutationResponseSchema.parse({
        api_key: "findb_srv_once",
        data: credential,
      }).api_key
    ).toBe("findb_srv_once")
    expect(
      userMutationResponseSchema.parse({
        data: { ...user, must_change_password: true },
        temporary_password: "one-time-password",
      }).temporary_password
    ).toBe("one-time-password")
  })
})

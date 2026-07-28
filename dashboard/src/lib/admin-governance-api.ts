import { z } from "zod"

const isoDateTime = z.string().datetime({ offset: true })
const nullableDateTime = isoDateTime.nullable()

export const adminRoleSchema = z.enum(["owner", "operator", "viewer"])
export type AdminRole = z.infer<typeof adminRoleSchema>

export const adminUserSchema = z.object({
  user_id: z.uuid(),
  username: z.string(),
  display_name: z.string(),
  role: adminRoleSchema,
  is_active: z.boolean(),
  must_change_password: z.boolean(),
  created_at: isoDateTime.optional(),
  updated_at: isoDateTime.optional(),
  last_login_at: nullableDateTime.optional(),
})
export type AdminUser = z.infer<typeof adminUserSchema>

export const adminSessionUserSchema = z
  .union([adminUserSchema, z.object({ user: adminUserSchema })])
  .transform(value => ("user" in value ? value.user : value))
export type AdminSessionUser = z.infer<typeof adminSessionUserSchema>

export const adminLoginResponseSchema = z.object({
  access_token: z.string().min(1),
  token_type: z.literal("bearer"),
  expires_at: isoDateTime,
  user: adminUserSchema,
})

export const usersResponseSchema = z.object({
  data: z.array(adminUserSchema),
})

export const userMutationResponseSchema = z.object({
  data: adminUserSchema,
  temporary_password: z.string().min(1).optional(),
})

export const credentialKindSchema = z.enum([
  "source",
  "serve",
  "admin",
  "legacy",
])
export const credentialStatusSchema = z.enum([
  "active",
  "expiring",
  "expired",
  "revoked",
  "legacy",
])
export type CredentialKind = z.infer<typeof credentialKindSchema>
export type CredentialStatus = z.infer<typeof credentialStatusSchema>

export const credentialSchema = z.object({
  credential_ref: z.string(),
  id: z.uuid().nullable(),
  kind: credentialKindSchema,
  name: z.string(),
  owner: z.string().nullable(),
  description: z.string().nullable(),
  status: credentialStatusSchema,
  fingerprint: z.string().nullable(),
  role: adminRoleSchema.nullable(),
  scopes: z.array(z.string()).nullable(),
  policies: z.record(z.string(), z.json()).nullable(),
  created_at: nullableDateTime,
  expires_at: nullableDateTime,
  revoked_at: nullableDateTime,
  last_used_at: nullableDateTime,
  usage_count: z.number().int().nonnegative(),
  rotated_from_id: z.uuid().nullable(),
})
export type Credential = z.infer<typeof credentialSchema>

export const credentialsResponseSchema = z.object({
  data: z.array(credentialSchema),
})

export const credentialMutationResponseSchema = z.object({
  api_key: z.string().min(1),
  data: credentialSchema,
})

export const credentialRevokeResponseSchema = z
  .union([credentialSchema, z.object({ data: credentialSchema })])
  .transform(value => ("data" in value ? value.data : value))

export const credentialsOverviewSchema = z.object({
  auth_mode: z.string(),
  serve_require_auth: z.boolean(),
  legacy: z.object({
    admin: z.boolean(),
    source: z.boolean(),
    serve: z.boolean(),
  }),
  counts: z.object({
    active: z.number().int().nonnegative(),
    expiring: z.number().int().nonnegative(),
    expired: z.number().int().nonnegative(),
    revoked: z.number().int().nonnegative(),
    legacy: z.number().int().nonnegative(),
  }),
  usage_updated_at: nullableDateTime,
})
export type CredentialsOverview = z.infer<typeof credentialsOverviewSchema>

const optionalText = z
  .string()
  .trim()
  .max(500)
  .optional()
  .transform(value => value || undefined)
const optionalPositiveInteger = z.number().int().positive().optional()
const optionalExpiresAt = z
  .union([isoDateTime, z.literal("")])
  .optional()
  .transform(value => value || undefined)

export const credentialFiltersSchema = z.object({
  kind: z.union([credentialKindSchema, z.literal("")]).default(""),
  status: z.union([credentialStatusSchema, z.literal("")]).default(""),
  owner: z.string().trim().max(200).default(""),
})
export type CredentialFilters = z.infer<typeof credentialFiltersSchema>

export const createCredentialSchema = z.discriminatedUnion("kind", [
  z.object({
    kind: z.literal("source"),
    name: z.string().trim().min(1).max(200),
    owner: z.string().trim().min(1).max(200),
    description: optionalText,
    source_name: z.string().trim().min(1).max(100),
    allowed_datasets: z.array(z.string().trim().min(1).max(200)).optional(),
    rate_limit_requests: optionalPositiveInteger,
    rate_limit_window: optionalPositiveInteger,
    expires_at: optionalExpiresAt,
  }),
  z.object({
    kind: z.literal("serve"),
    name: z.string().trim().min(1).max(200).optional(),
    owner: z.string().trim().min(1).max(200),
    description: optionalText,
    tier: optionalText,
    scopes: z.array(z.string().trim().min(1).max(200)).optional(),
    rate_limit_requests: optionalPositiveInteger,
    rate_limit_window: optionalPositiveInteger,
    page_size_limit: optionalPositiveInteger,
    expires_at: optionalExpiresAt,
  }),
  z.object({
    kind: z.literal("admin"),
    name: z.string().trim().min(1).max(200).optional(),
    owner: z.string().trim().min(1).max(200),
    description: optionalText,
    role: adminRoleSchema,
    scopes: z.array(z.string().trim().min(1).max(200)).optional(),
    rate_limit_requests: optionalPositiveInteger,
    rate_limit_window: optionalPositiveInteger,
    page_size_limit: optionalPositiveInteger,
    expires_at: optionalExpiresAt,
  }),
])
export type CreateCredential = z.infer<typeof createCredentialSchema>

export const credentialTargetSchema = z.object({
  kind: z.enum(["source", "serve", "admin"]),
  id: z.uuid(),
})

export const createUserSchema = z.object({
  username: z.string().trim().min(1).max(200),
  display_name: z.string().trim().min(1).max(200),
  role: adminRoleSchema,
  password: optionalText,
  must_change_password: z.boolean().optional().default(true),
})

export const updateUserSchema = z.object({
  userId: z.uuid(),
  display_name: z.string().trim().min(1).max(200).optional(),
  role: adminRoleSchema.optional(),
  is_active: z.boolean().optional(),
})

export const resetUserPasswordSchema = z.object({
  userId: z.uuid(),
  password: optionalText,
})

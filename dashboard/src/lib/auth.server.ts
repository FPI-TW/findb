import {
  createHash,
  createHmac,
  randomBytes,
  timingSafeEqual,
} from "node:crypto"
import { basename, resolve } from "node:path"

import { config as loadDotenv } from "dotenv"
import {
  deleteCookie,
  getCookie,
  getRequest,
  setCookie,
  setResponseHeader,
} from "@tanstack/react-start/server"

const SESSION_COOKIE = "findb_dashboard_session"
const SESSION_TTL_SECONDS = 8 * 60 * 60
const LOGIN_WINDOW_MS = 60_000
const LOGIN_ATTEMPTS = 5
const loginAttempts = new Map<string, number[]>()

type DashboardConfig = {
  adminApiKey: string
  apiBaseUrl: string
  username: string
  password: string
  sessionSecret: string
}

type SessionPayload = {
  username: string
  expiresAt: number
  nonce: string
}

function required(name: string, value: string | undefined, minimum = 1) {
  const normalized = value?.trim() ?? ""
  if (normalized.length < minimum) {
    throw new Error(`${name} is not configured`)
  }
  return normalized
}

export function configFromEnvironment(
  environment: NodeJS.ProcessEnv
): DashboardConfig {
  return {
    adminApiKey: required("ADMIN_API_KEY", environment.ADMIN_API_KEY),
    apiBaseUrl:
      environment.FINDB_API_BASE_URL?.trim() || "http://localhost:8080",
    username: required("DASHBOARD_USERNAME", environment.DASHBOARD_USERNAME),
    password: required("DASHBOARD_PASSWORD", environment.DASHBOARD_PASSWORD),
    sessionSecret: required(
      "DASHBOARD_SESSION_SECRET",
      environment.DASHBOARD_SESSION_SECRET,
      32
    ),
  }
}

export function getDashboardConfig() {
  const cwd = process.cwd()
  const repositoryRoot =
    basename(cwd) === "dashboard" ? resolve(cwd, "..") : cwd
  loadDotenv({
    path: resolve(repositoryRoot, ".env"),
    override: false,
    quiet: true,
  })
  return configFromEnvironment(process.env)
}

function digest(value: string) {
  return createHash("sha256").update(value).digest()
}

export function credentialsMatch(
  submittedUsername: string,
  submittedPassword: string,
  config: Pick<DashboardConfig, "username" | "password">
) {
  const usernameMatches = timingSafeEqual(
    digest(submittedUsername),
    digest(config.username)
  )
  const passwordMatches = timingSafeEqual(
    digest(submittedPassword),
    digest(config.password)
  )
  return usernameMatches && passwordMatches
}

function signature(payload: string, config: DashboardConfig) {
  return createHmac("sha256", config.sessionSecret)
    .update(payload)
    .update("\0")
    .update(config.username)
    .update("\0")
    .update(config.password)
    .digest("base64url")
}

export function createSessionToken(config: DashboardConfig, now = Date.now()) {
  const payload: SessionPayload = {
    username: config.username,
    expiresAt: now + SESSION_TTL_SECONDS * 1000,
    nonce: randomBytes(18).toString("base64url"),
  }
  const encoded = Buffer.from(JSON.stringify(payload)).toString("base64url")
  return `${encoded}.${signature(encoded, config)}`
}

export function verifySessionToken(
  token: string | undefined,
  config: DashboardConfig,
  now = Date.now()
) {
  if (!token) return null
  const separator = token.lastIndexOf(".")
  if (separator <= 0) return null
  const encoded = token.slice(0, separator)
  const receivedSignature = token.slice(separator + 1)
  const expectedSignature = signature(encoded, config)
  const receivedBytes = Buffer.from(receivedSignature, "base64url")
  const expectedBytes = Buffer.from(expectedSignature, "base64url")
  if (
    receivedBytes.toString("base64url") !== receivedSignature ||
    receivedBytes.length !== expectedBytes.length ||
    !timingSafeEqual(receivedBytes, expectedBytes)
  ) {
    return null
  }
  try {
    const payload = JSON.parse(
      Buffer.from(encoded, "base64url").toString("utf8")
    ) as SessionPayload
    if (
      payload.username !== config.username ||
      !Number.isFinite(payload.expiresAt) ||
      payload.expiresAt <= now
    ) {
      return null
    }
    return { username: payload.username, expiresAt: payload.expiresAt }
  } catch {
    return null
  }
}

function cookieOptions() {
  return {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "strict" as const,
    path: "/dashboard",
    maxAge: SESSION_TTL_SECONDS,
  }
}

export function issueDashboardSession(config: DashboardConfig) {
  setCookie(SESSION_COOKIE, createSessionToken(config), cookieOptions())
}

export function clearDashboardSession() {
  deleteCookie(SESSION_COOKIE, cookieOptions())
}

export function getDashboardSession(config = getDashboardConfig()) {
  return verifySessionToken(getCookie(SESSION_COOKIE), config)
}

export function requireDashboardSession(config = getDashboardConfig()) {
  const session = getDashboardSession(config)
  if (!session) throw new Error("Unauthorized")
  return session
}

export function assertSameOrigin() {
  const request = getRequest()
  const origin = request.headers.get("origin")
  if (!origin) return
  const forwardedProto = request.headers.get("x-forwarded-proto")
  const forwardedHost = request.headers.get("x-forwarded-host")
  const expectedOrigin = `${forwardedProto || new URL(request.url).protocol.replace(":", "")}://${forwardedHost || request.headers.get("host")}`
  if (new URL(origin).origin !== expectedOrigin) {
    throw new Error("Origin check failed")
  }
}

export function enforceLoginRateLimit(now = Date.now()) {
  const request = getRequest()
  const forwarded = request.headers.get("x-forwarded-for")
  const identity =
    request.headers.get("cf-connecting-ip") ||
    forwarded?.split(",")[0]?.trim() ||
    "unknown"
  const recent = (loginAttempts.get(identity) ?? []).filter(
    timestamp => timestamp > now - LOGIN_WINDOW_MS
  )
  if (recent.length >= LOGIN_ATTEMPTS) {
    throw new Error("登入嘗試過於頻繁，請稍後再試。")
  }
  recent.push(now)
  loginAttempts.set(identity, recent)
  if (loginAttempts.size > 10_000) {
    const oldestIdentity = loginAttempts.keys().next().value
    if (oldestIdentity) loginAttempts.delete(oldestIdentity)
  }
}

export function markPrivateResponse() {
  setResponseHeader("Cache-Control", "no-store")
  setResponseHeader("Vary", "Cookie")
}

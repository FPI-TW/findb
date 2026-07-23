type BrowserLocation = Pick<Location, "hostname" | "port" | "protocol">

export function resolvePublicApiUrl(
  path: string,
  location: BrowserLocation = window.location
) {
  if (location.port === "3000" || location.port === "3333") {
    return new URL(
      path,
      `${location.protocol}//${location.hostname}:8080`
    ).toString()
  }
  return path
}

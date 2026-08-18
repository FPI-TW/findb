import {
  HeadContent,
  Link,
  Scripts,
  createRootRoute,
} from "@tanstack/react-router"
import { ArrowLeft, SearchX } from "lucide-react"

import { Button } from "../components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "../components/ui/card"
import { Toaster } from "../components/ui/toast"
import Footer from "../components/Footer"
import Header from "../components/Header"
import { QueryProvider } from "../components/QueryProvider"

import appCss from "../styles.css?url"

const THEME_INIT_SCRIPT = `(function(){try{var stored=window.localStorage.getItem('theme');var mode=(stored==='light'||stored==='dark'||stored==='auto')?stored:'auto';var prefersDark=window.matchMedia('(prefers-color-scheme: dark)').matches;var resolved=mode==='auto'?(prefersDark?'dark':'light'):mode;var root=document.documentElement;root.classList.remove('light','dark');root.classList.add(resolved);if(mode==='auto'){root.removeAttribute('data-theme')}else{root.setAttribute('data-theme',mode)}root.style.colorScheme=resolved;}catch(e){}})();`

export const Route = createRootRoute({
  head: () => ({
    meta: [
      {
        charSet: "utf-8",
      },
      {
        name: "viewport",
        content: "width=device-width, initial-scale=1",
      },
      {
        title: "FinDB Operations Console",
      },
    ],
    links: [
      {
        rel: "stylesheet",
        href: appCss,
      },
    ],
  }),
  notFoundComponent: NotFoundPage,
  shellComponent: RootDocument,
})

export function NotFoundPage() {
  return (
    <main className="mx-auto grid min-h-[calc(100vh-10rem)] w-full max-w-screen-2xl place-items-center px-4 py-14 sm:px-6 lg:px-8">
      <Card className="w-full max-w-xl text-center">
        <CardHeader className="items-center">
          <span className="mb-2 inline-flex size-12 items-center justify-center rounded-2xl bg-accent-soft text-accent">
            <SearchX aria-hidden="true" />
          </span>
          <p className="font-mono text-xs font-medium tracking-widest text-accent uppercase">
            Error 404
          </p>
          <CardTitle asChild className="text-3xl tracking-tight">
            <h1>找不到這個頁面</h1>
          </CardTitle>
          <CardDescription className="max-w-md leading-6">
            網址可能有誤，或頁面已經移動。你可以返回 FinDB 首頁重新選擇功能。
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button asChild>
            <Link to="/">
              <ArrowLeft aria-hidden="true" />
              返回首頁
            </Link>
          </Button>
        </CardContent>
      </Card>
    </main>
  )
}

function RootDocument({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-Hant" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
        <HeadContent />
      </head>
      <body className="font-sans antialiased wrap-anywhere selection:bg-[rgba(79,184,178,0.24)]">
        <Header />
        <QueryProvider>{children}</QueryProvider>
        <Footer />
        <Toaster />
        <Scripts />
      </body>
    </html>
  )
}

import { createContext, useContext } from "react"

const ProtectedQueryScopeContext = createContext("protected")

export function ProtectedQueryScopeProvider({
  value,
  children,
}: {
  value: string
  children: React.ReactNode
}) {
  return (
    <ProtectedQueryScopeContext.Provider value={value}>
      {children}
    </ProtectedQueryScopeContext.Provider>
  )
}

export function useProtectedQueryScope() {
  return useContext(ProtectedQueryScopeContext)
}

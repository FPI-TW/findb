import { createFileRoute } from "@tanstack/react-router"

import SkillPage from "../features/skill/SkillPage"

export const Route = createFileRoute("/skill")({
  head: () => ({
    meta: [
      { title: "FinDB Skill 安裝教學" },
      {
        name: "description",
        content: "下載並安裝 FinDB API Skill 到 Claude Code、Codex 或 Cursor。",
      },
    ],
  }),
  component: SkillPage,
})

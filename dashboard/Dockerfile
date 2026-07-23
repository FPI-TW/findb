FROM node:22-alpine AS build

WORKDIR /workspace
RUN corepack enable

COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY dashboard/package.json dashboard/package.json
RUN pnpm install --frozen-lockfile

COPY dashboard dashboard
RUN pnpm --filter dashboard build

FROM node:22-alpine AS runtime

WORKDIR /app
ENV NODE_ENV=production
ENV HOST=0.0.0.0
ENV PORT=3333

COPY --from=build /workspace/dashboard/.output ./.output

EXPOSE 3333
CMD ["node", ".output/server/index.mjs"]

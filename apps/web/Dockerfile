# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Build stage
# ---------------------------------------------------------------------------
FROM node:20-alpine AS build

WORKDIR /app

# Ensure the static assets directory exists so the production stage COPY never
# fails when a public folder has not been added yet.
RUN mkdir -p public

COPY package.json package-lock.json* ./

# Install dependencies for a production build
RUN npm ci || npm install

COPY . .

# Next.js bakes rewrites into the build at `next build` time, so the proxy
# destination must be available in this build stage. In Docker Compose it is
# passed via build.args; default keeps the local-dev value.
ARG API_PROXY_TARGET=http://localhost:8000
ENV API_PROXY_TARGET=$API_PROXY_TARGET

# TypeScript compile + Next.js production build
RUN npm run build

# ---------------------------------------------------------------------------
# Production stage
# ---------------------------------------------------------------------------
FROM node:20-alpine AS production

WORKDIR /app

ENV NODE_ENV=production

# Copy the lockfile and package.json, then install only production deps.
COPY package.json package-lock.json* ./
RUN npm ci --omit=dev || npm install --omit=dev

# Drop Next.js's own test tooling from the runtime image.
#
# `next` 14 lists `@playwright/test` as a real dependency (it uses Playwright for
# its own test suite), so `--omit=dev` does **not** remove it: the production image
# shipped playwright, playwright-core and @playwright/test — tens of megabytes of
# browser-automation tooling in a web server, with no runtime use for any of it.
# The supply-chain rule for this image is "production dependencies only", and this
# is what makes that true. Verified by the check in .github/workflows/ci.yml,
# which fails if any of the three reappears.
RUN rm -rf node_modules/@playwright node_modules/playwright node_modules/playwright-core

# Copy the build output and config files.
COPY --from=build /app/.next ./.next
COPY --from=build /app/public ./public
COPY --from=build /app/next.config.mjs ./
COPY --from=build /app/package.json ./package.json

# Next.js collects anonymous telemetry; keep it off in the container.
ENV NEXT_TELEMETRY_DISABLED=1

EXPOSE 3000

USER node

CMD ["npm", "start"]
# Caddy with the built admin UI inside it (EAP-66). Build context is the repository root.
#
# Stage 1 builds web/ exactly as `npm run build` does locally, with VITE_API_BASE set to the
# empty string: the API is then fetched from the page's own origin, which is Caddy, which
# proxies /v1 to the API. No hostname is baked into the bundle, so one image serves any
# domain - the domain lives in .env.production and reaches Caddy at run time.

FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
ENV VITE_API_BASE=
RUN npm run build

FROM caddy:2-alpine
COPY deploy/Caddyfile /etc/caddy/Caddyfile
COPY --from=web /web/dist /srv/web

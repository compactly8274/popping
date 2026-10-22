# --- Build stage ------------------------------------------------------------
# Also the target the dev override runs (``npm run dev`` with a bind mount).
FROM node:22.20-alpine AS build

WORKDIR /app

# Install deps first so source changes don't bust the cache. ``npm ci``
# installs exactly what package-lock.json pins.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
# ``npm run build`` = prebuild HTML-comment check, ``tsc``, ``vite build``.
RUN npm run build

# --- Runtime stage ----------------------------------------------------------
# Static files + reverse proxy. Runs as an unprivileged user on port 8080.
FROM nginxinc/nginx-unprivileged:1.27.5-alpine

ENV BACKEND_URL=http://backend:8000
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 8080

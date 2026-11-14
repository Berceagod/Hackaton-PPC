# Build context = repository root.
# Stage 1 builds the (teammates') React SPA with the mock turned OFF.
FROM node:22-alpine AS frontend
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ .
ARG VITE_USE_MOCK=false
ARG VITE_API_URL=/api/v1
ENV VITE_USE_MOCK=$VITE_USE_MOCK VITE_API_URL=$VITE_API_URL
RUN npm run build

# Stage 2: gateway = TLS + SPA + API proxy
FROM nginx:1.27-alpine
RUN apk add --no-cache openssl
RUN rm -f /etc/nginx/conf.d/default.conf
COPY infrastructure/nginx/default.conf /etc/nginx/conf.d/filegrid.conf
COPY infrastructure/nginx/40-gen-cert.sh /docker-entrypoint.d/40-gen-cert.sh
RUN chmod +x /docker-entrypoint.d/40-gen-cert.sh
COPY --from=frontend /app/dist /usr/share/nginx/html
EXPOSE 80 443
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
  CMD wget -qO- --no-check-certificate https://127.0.0.1/healthz || exit 1

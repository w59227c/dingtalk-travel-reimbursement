FROM node:24.20.0-alpine@sha256:e67514e5d0f6c46656005e1b693b2ec9d52e80b641307de684d4a015ba7a4eaf AS build

WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM nginx:1.30.4-alpine@sha256:dc5069ad14f19660b141b21236140b91656bf89bbc3e2417c70ae650cd66104c AS runtime
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx/default.conf /etc/nginx/conf.d/default.conf
COPY nginx/expense-proxy.conf /etc/nginx/conf.d/expense-proxy.inc

# The application Nginx does not need root or a privileged port. Compose adds
# read-only/rootfs and tmpfs constraints for its only writable runtime paths.
USER nginx
EXPOSE 8080
ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]

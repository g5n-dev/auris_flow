# syntax=docker/dockerfile:1.7
FROM node:22.16.0-alpine3.22 AS ui-builder
WORKDIR /src
COPY prototype/auris-flow-ui/package.json prototype/auris-flow-ui/package-lock.json ./
RUN npm ci --ignore-scripts
COPY prototype/auris-flow-ui ./
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.28.0-alpine3.21 AS runtime
COPY --from=ui-builder /src/dist /usr/share/nginx/html
COPY production/edge/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080 8443

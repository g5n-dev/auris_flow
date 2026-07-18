# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
FROM node:22.16.0-alpine3.22@sha256:41e4389f3d988d2ed55392df4db1420ad048ae53324a8e2b7c6d19508288107e AS ui-builder
WORKDIR /src
COPY prototype/auris-flow-ui/package.json prototype/auris-flow-ui/package-lock.json ./
RUN npm ci --ignore-scripts
COPY prototype/auris-flow-ui ./
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.28.0-alpine3.21@sha256:c97ff0bf7cbae369953c6da1232ec14ad9f971d66360c5698db0856a4cd657a0 AS runtime
COPY --from=ui-builder /src/dist /usr/share/nginx/html
COPY production/edge/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080 443

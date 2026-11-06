FROM --platform=$BUILDPLATFORM node:20-slim AS build
WORKDIR /app
COPY package.json .
RUN npm install
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
# Rendered to /etc/nginx/conf.d/default.conf by the image's entrypoint -
# see the comments in nginx.conf.template.
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
ENV NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1 \
    BACKEND_UPSTREAM=http://backend:8000

EXPOSE 80

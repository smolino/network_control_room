FROM --platform=$BUILDPLATFORM node:20-slim AS build
WORKDIR /app
COPY package.json .
RUN npm install
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/templates/nginx.ssl.conf
COPY nginx.dev.conf /etc/nginx/templates/nginx.dev.conf
COPY start.sh /start.sh
RUN chmod +x /start.sh

# Set to "true" for local dev, where no Let's Encrypt certificate is
# available - serves plain HTTP instead (see start.sh / nginx.dev.conf).
ENV DISABLE_SSL=false

EXPOSE 80 443
CMD ["/start.sh"]

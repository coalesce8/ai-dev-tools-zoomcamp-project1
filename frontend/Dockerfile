FROM node:24-alpine AS build
WORKDIR /frontend
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html tsconfig.json vite.config.ts ./
COPY src/ src/
RUN npm run build

FROM nginx:1.27-alpine
# The image's entrypoint runs envsubst over /etc/nginx/templates/*.template.
ENV API_UPSTREAM=http://api:8000
ENV NGINX_ENVSUBST_FILTER=API_UPSTREAM
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /frontend/dist /usr/share/nginx/html
EXPOSE 8080

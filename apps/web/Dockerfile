# drHiro web + Telegram Mini App — React/Vite SPA
# VITE_API_BASE  : backend root the SPA calls, e.g. /drhiro/api/v1
# VITE_BASE_PATH : URL subpath the SPA is served under, e.g. /drhiro-app/
FROM node:20-alpine AS build
ARG VITE_API_BASE=/api/v1
ARG VITE_BASE_PATH=/
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci || npm install
COPY . .
RUN VITE_API_BASE=$VITE_API_BASE VITE_BASE_PATH=$VITE_BASE_PATH npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 3000

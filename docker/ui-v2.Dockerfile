# Stage 1: Build React app
FROM node:20-alpine AS build
WORKDIR /app
COPY legba-ui/package.json legba-ui/package-lock.json* ./
RUN npm install
COPY legba-ui/ .
RUN npm run build

# Stage 2: Serve with nginx
FROM nginx:alpine
COPY docker/nginx-ui-v2.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8503
CMD ["nginx", "-g", "daemon off;"]

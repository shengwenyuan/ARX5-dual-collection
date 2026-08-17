FROM node:24-alpine AS prebuilt

ENV NODE_ENV=production
WORKDIR /app
COPY --chown=node:node server.mjs ./server.mjs
COPY --chown=node:node dist ./dist
USER node
EXPOSE 4173
CMD ["node", "server.mjs"]

FROM node:24-alpine AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html tsconfig.json vite.config.ts ./
COPY src ./src
RUN npm run build

FROM node:24-alpine AS production

ENV NODE_ENV=production
WORKDIR /app
COPY --chown=node:node server.mjs ./server.mjs
COPY --from=build --chown=node:node /app/dist ./dist
USER node
EXPOSE 4173
CMD ["node", "server.mjs"]

FROM node:24-slim AS build
WORKDIR /app
COPY control-center/web/package*.json ./control-center/web/
RUN npm ci --prefix control-center/web
COPY control-center/web ./control-center/web
RUN npm run build --prefix control-center/web

COPY control-center/worker/package*.json ./control-center/worker/
RUN npm ci --prefix control-center/worker
COPY control-center/worker ./control-center/worker
RUN npm run build:node --prefix control-center/worker

FROM node:24-slim
WORKDIR /app
COPY config/pools.json ./config/pools.json
COPY control-center/worker/package.json ./control-center/worker/package.json
COPY control-center/worker/migrations ./control-center/worker/migrations
COPY control-center/worker/scripts/seed.mjs ./control-center/worker/scripts/seed.mjs
COPY control-center/worker/scripts/start-node-server.mjs ./control-center/worker/scripts/start-node-server.mjs
COPY control-center/worker/src/storage/sqlStore.ts ./control-center/worker/src/storage/sqlStore.ts
COPY control-center/worker/src/adapters/nodeSqlite.ts ./control-center/worker/src/adapters/nodeSqlite.ts
COPY control-center/worker/src/adapters/nodeServer.ts ./control-center/worker/src/adapters/nodeServer.ts
COPY --from=build /app/control-center/worker/dist/app.mjs ./control-center/worker/dist/app.mjs
COPY --from=build /app/control-center/web/dist/client ./control-center/web/dist/client

ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=8788 \
    DB_PATH=/data/control-center.sqlite \
    STATIC_DIR=/app/control-center/web/dist/client

VOLUME ["/data"]
EXPOSE 8788
CMD ["sh", "-c", "node control-center/worker/scripts/seed.mjs --sqlite \"$DB_PATH\" && exec node --experimental-strip-types control-center/worker/scripts/start-node-server.mjs"]

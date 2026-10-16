# Evolution API 2.3.7 + Baileys 7.0.0-rc14 + omitel-evolution.patch (portado de deploy/evolution).
# Candidata para reemplazar deploy/evolution (2.2.3 + Baileys 6.7.12) si WhatsApp Business sigue
# sin vincular. rc14 ya trae el companion_platform_id y el historySyncConfig correctos; solo falta
# companion_reg_refresh (patch-baileys-pairing.js).
FROM node:24-alpine AS builder

# Se compila en el mismo droplet de producción: sin tope, tsc/tsup agotan la RAM y tumban la app.
ENV NODE_OPTIONS=--max-old-space-size=1280

RUN apk add --no-cache git ffmpeg wget curl bash openssl dos2unix

WORKDIR /evolution

RUN git clone --depth 1 --branch 2.3.7 https://github.com/EvolutionAPI/evolution-api.git .

COPY omitel-evolution.patch /tmp/omitel-evolution.patch
RUN git apply /tmp/omitel-evolution.patch

RUN npm pkg set dependencies.baileys=7.0.0-rc14 && \
    npm install --no-audit --no-fund && \
    node -e "const v=require('./node_modules/baileys/package.json').version; if (v!=='7.0.0-rc14') { throw new Error(v); }"

COPY patch-baileys-pairing.js /tmp/patch-baileys-pairing.js
RUN node /tmp/patch-baileys-pairing.js && \
    grep -q "companion_reg_refresh" node_modules/baileys/lib/Socket/socket.js && \
    node --check node_modules/baileys/lib/Socket/socket.js && \
    node --check node_modules/baileys/lib/Socket/messages-recv.js

RUN cp .env.example .env && chmod +x ./Docker/scripts/* && dos2unix ./Docker/scripts/*

RUN ./Docker/scripts/generate_database.sh

RUN npm run build

FROM node:24-alpine AS final

RUN apk add --no-cache tzdata ffmpeg bash openssl

ENV TZ=America/Sao_Paulo

WORKDIR /evolution

COPY --from=builder /evolution/package.json ./package.json
COPY --from=builder /evolution/package-lock.json ./package-lock.json
COPY --from=builder /evolution/node_modules ./node_modules
COPY --from=builder /evolution/dist ./dist
COPY --from=builder /evolution/prisma ./prisma
COPY --from=builder /evolution/manager ./manager
COPY --from=builder /evolution/public ./public
COPY --from=builder /evolution/.env ./.env
COPY --from=builder /evolution/Docker ./Docker
COPY --from=builder /evolution/runWithProvider.js ./runWithProvider.js
COPY --from=builder /evolution/tsup.config.ts ./tsup.config.ts

ENV DOCKER_ENV=true

EXPOSE 8080

ENTRYPOINT ["/bin/bash", "-c", ". ./Docker/scripts/deploy_database.sh && npm run start:prod" ]

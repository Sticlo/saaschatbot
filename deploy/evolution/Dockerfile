# Evolution API 2.2.3 oficial + omitel-evolution.patch (envío a contactos @lid y /chat/chatStates).
# Es la misma versión que corre en local (whatsapp/evolution-api); al cambiar el parche allá,
# regenerarlo con: git -C whatsapp/evolution-api diff > deploy/evolution/omitel-evolution.patch
FROM node:20-alpine AS builder

RUN apk add --no-cache git ffmpeg wget curl bash openssl dos2unix

# El lockfile apunta al fork de Baileys por ssh; en la imagen no hay llaves, se baja por https.
RUN git config --global --add url."https://github.com/".insteadOf "ssh://git@github.com/" && \
    git config --global --add url."https://github.com/".insteadOf "git@github.com:"

WORKDIR /evolution

RUN git clone --depth 1 --branch 2.2.3 https://github.com/EvolutionAPI/evolution-api.git .

COPY omitel-evolution.patch /tmp/omitel-evolution.patch
RUN git apply /tmp/omitel-evolution.patch

RUN npm ci

RUN cp .env.example .env && chmod +x ./Docker/scripts/* && dos2unix ./Docker/scripts/*

RUN ./Docker/scripts/generate_database.sh

RUN npm run build

FROM node:20-alpine AS final

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

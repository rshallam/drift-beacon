# syntax=docker/dockerfile:1.4

ARG NODE_VERSION=24.13.0
ARG BUILD_FROM
ARG SERVER_IMAGE=ghcr.io/rshallam/drift-beacon-server:0.1.0

# Pull the pre-built server image
FROM ${SERVER_IMAGE} AS server

# ============================================
# Runtime: HA Addon
# ============================================
FROM ${BUILD_FROM} AS runtime

ARG BUILD_ARCH
ARG NODE_VERSION
ARG TEMPIO_VERSION="2024.11.2"

ENV SERVICE_PORT=9823
ENV NGINX_PORT_HTTPS=9000
ENV NGINX_PORT_HTTP=9001

WORKDIR /app

RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    jq \
    nginx \
    openssl \
    && rm -rf /var/lib/apt/lists/* /etc/nginx \
    && ARCH= && \
    case "$(dpkg --print-architecture)" in \
        arm64) ARCH='arm64' ;; \
        amd64) ARCH='x64' ;; \
        armhf) ARCH='armv7l' ;; \
        *) echo "Unsupported architecture" && exit 1 ;; \
    esac && \
    NODE_URL="https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${ARCH}.tar.xz" && \
    echo "Downloading Node.js from: ${NODE_URL}" && \
    curl -fsSL "${NODE_URL}" -o node.tar.xz && \
    tar -xJf node.tar.xz -C /usr/local --strip-components=1 && \
    rm node.tar.xz
    # Note: curl is kept installed for bashio to contact HA API

# Install tempio for nginx config templating
RUN curl -L -s -o /usr/bin/tempio \
    "https://github.com/home-assistant/tempio/releases/download/${TEMPIO_VERSION}/tempio_${BUILD_ARCH}" \
    && chmod a+x /usr/bin/tempio

# Copy pre-built server artifacts from GHCR
COPY --from=server /app ./

# Copy rootfs for Home Assistant S6 supervision
COPY rootfs /

# Make scripts executable
RUN chmod +x /usr/local/bin/*.sh \
    && chmod +x /etc/s6-overlay/s6-rc.d/*/run

# Create data directory for persistence
RUN mkdir -p /data

# Set environment
ENV NODE_ENV=production \
    PORT=9823 \
    DATA_DIR=/data

# Expose ports
EXPOSE 9000
EXPOSE 9001

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
    CMD node -e "require('http').get('http://localhost:${PORT}/api/device/status', (r) => {let d='';r.on('data',c=>d+=c);r.on('end',()=>{try{const j=JSON.parse(d);process.exit(j.status==='healthy'?0:1)}catch(e){process.exit(1)}}).on('error',()=>process.exit(1))})" || exit 1

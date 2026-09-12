FROM artifactory.corp/docker-remote/node@sha256:a1b2c3d4e5f60718293a4b5c6d7e8f90112233445566778899aabbccddeeff00

WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .

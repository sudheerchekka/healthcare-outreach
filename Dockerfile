FROM python:3.12-slim

# Install Node.js 20
RUN apt-get update && apt-get install -y curl gnupg && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps (vendor/ must be present before building — see README)
COPY vendor/ ./vendor/
COPY src/tac/requirements.txt ./src/tac/requirements.txt
RUN pip install --no-cache-dir -r src/tac/requirements.txt

# Node.js deps + TypeScript build
COPY package*.json ./
RUN npm ci
COPY src/ ./src/
COPY tsconfig.json ./
RUN npm run build

# Copy per-app client/ dirs and app.json configs into the compiled output tree
RUN find src/apps -maxdepth 2 \( -name client -type d \) | while read d; do \
      dest="dist/${d#src/}"; \
      mkdir -p "$dest" && cp -r "$d/." "$dest/"; \
    done && \
    find src/apps -maxdepth 2 -name app.json | while read f; do \
      dest="dist/${f#src/}"; \
      mkdir -p "$(dirname $dest)" && cp "$f" "$dest"; \
    done

EXPOSE 8000 8001

COPY start.sh ./
RUN chmod +x start.sh

CMD ["./start.sh"]

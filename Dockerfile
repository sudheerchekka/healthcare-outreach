FROM node:20-slim

WORKDIR /app

# Install dependencies first (layer-cached)
COPY package*.json ./
RUN npm ci --omit=dev

# Copy compiled output and static assets
COPY dist/ ./dist/
COPY healthcare_outreach/ ./healthcare_outreach/

EXPOSE 8000

ENV PORT=8000

CMD ["node", "dist/app.js"]

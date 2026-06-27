FROM node:22-alpine

WORKDIR /app

# Install deps first so source changes don't bust the cache
COPY package.json ./
RUN npm install

COPY . .

EXPOSE 5173

# Dev server with HMR; production would build + serve via nginx.
CMD ["npm", "run", "dev"]
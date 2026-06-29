FROM node:22-alpine

WORKDIR /app

# Install deps first so source changes don't bust the cache
COPY package.json ./
RUN npm install

# Guard against the "JSX-style /* ... */ comments in index.html render
# as visible text" regression (commits 32bdd2f introduced, 621d90c
# fixed). Wired into ``npm run build`` via ``prebuild``, but the
# production image runs ``npm run dev`` so ``prebuild`` never fires
# there — invoke the script directly after the source copy so a bad
# ``index.html`` fails the docker build before the layer is committed.
COPY scripts ./scripts
COPY index.html ./
RUN node scripts/check-html-comments.mjs

COPY . .

EXPOSE 5173

# Dev server with HMR; production would build + serve via nginx.
CMD ["npm", "run", "dev"]
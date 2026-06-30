FROM node:22-alpine

WORKDIR /app

# Install deps first so source changes don't bust the cache
COPY package.json ./
RUN npm install

# Guard against the "JSX-style /* ... */ comments in index.html render
# as visible text" regression (commits 32bdd2f introduced, 621d90c
# fixed). Wired into ``npm run build`` via ``prebuild``, but we run
# it here too as a belt-and-braces build-time check that fires even
# if a future maintainer moves the build step.
COPY scripts ./scripts
COPY index.html ./
RUN node scripts/check-html-comments.mjs

# Build the production bundle into ``dist/``. The dev CMD was a
# long-standing misnomer (``npm run dev`` runs the Vite dev server,
# which has HMR + watchfiles + a slow first-render) and was the root
# cause of the "spinner → quick view → black" reload flicker on
# F5. ``npm run build`` runs ``tsc && vite build`` (the prebuild
# check above is redundant in the same chain but cheap). The
# resulting ``dist/`` is what ``npm run preview`` serves.
COPY . .
RUN npm run build

EXPOSE 5173

# Production CMD: serve the built ``dist/`` via ``vite preview``.
# Same port as the dev server (5173) so docker-compose's port
# mapping doesn't change. ``vite preview`` configures its proxy
# from ``vite.config.ts``'s ``preview`` block (mirror of
# ``server``), so /api and /assets are forwarded to the backend
# on the docker network the same way dev was.
CMD ["npm", "run", "preview"]
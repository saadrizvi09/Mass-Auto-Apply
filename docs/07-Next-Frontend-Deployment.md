# AutoApply Cloud — Next.js frontend deployment

The hosted UI is now a Next.js + React + TypeScript application. The source lives in
`frontend/`; the FastAPI control plane remains in `app/saas_main.py`.

## Where to make frontend changes

- `frontend/app/page.tsx` — authenticated workspace, navigation, views, forms, and UI behavior.
- `frontend/app/globals.css` — responsive layout and visual styles.
- `frontend/lib/api.ts` — authenticated and public API requests.
- `frontend/lib/types.ts` — shared frontend data contracts.
- `frontend/next.config.ts` — static-export and asset configuration.

## Local validation and static export

From the repository root:

```bash
npm --prefix frontend install
npm --prefix frontend run check
rm -rf public/assets
mkdir -p public/assets
cp -R frontend/out/. public/assets/
mv public/assets/index.html public/index.html
```

The checked-in `public/index.html` and `public/assets/_next/` are the production static
artifact. The asset prefix is `/assets`, so the frontend and API can be served by the
same root deployment without a separate frontend server.

## Deploy

```bash
vercel deploy --prod --yes
```

Keep the Vercel project root at the repository root. `vercel.json` configures the
FastAPI function and security headers; `.vercelignore` excludes local Next build
directories while retaining the checked-in static export. Do not add a Vercel build
command unless the generated asset bundle is verified against the committed
`public/index.html`.

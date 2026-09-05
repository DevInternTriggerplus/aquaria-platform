# Free Demo Deployment

This repository deploys without changing its application structure:

- `frontend-next/` is the Next.js customer site on Vercel Hobby.
- `backend/` is the Django API on Render Free.
- Neon Free supplies PostgreSQL.

This is a public demonstration environment, not a production ticket-sales system.
The API service sleeps after idle time on Render Free, checkout is not wired in the
Next.js client, and the backend has only a simulated payment gateway.

## 1. Create the database

1. Create a Neon Free project in the Singapore region.
2. Copy its pooled PostgreSQL connection string, including `sslmode=require`.
3. Keep the connection string private. It is the value Render prompts for as
   `DATABASE_URL`.

## 2. Deploy the API

1. Push this repository to GitHub.
2. In Render, create a Blueprint from the repository. Render reads `render.yaml`.
3. Enter the Neon connection string for `DATABASE_URL` when prompted.
4. Apply the Blueprint. It installs dependencies, collects Django static files,
   runs migrations, and performs the idempotent initial seed.
5. Wait until `https://<render-service>.onrender.com/api/health/` returns
   `{ "status": "ok" }`.

Render generates `SECRET_KEY` and `TICKET_SIGNING_KEY`; never replace either after
issuing tickets because changing the ticket-signing key invalidates issued QR codes.

## 3. Deploy the website

1. Import the same GitHub repository into Vercel Hobby.
2. Set the Vercel project Root Directory to `frontend-next`.
3. Add the production environment variable:

   ```text
   BACKEND_ORIGIN=https://<render-service>.onrender.com
   ```

4. Deploy. The Next.js rewrite proxies browser `/api/*` requests to Django, so the
   customer browser remains on the Vercel site origin.

## Verification

1. Open the Vercel URL and select a visit date.
2. Confirm venue, products, and prices load.
3. Open `<vercel-url>/api/health/` and confirm the proxied response is `status: ok`.
4. After 15 minutes idle, expect the first API request to Render Free to take about
   a minute while it wakes.

## Free-tier limits

- Vercel Hobby is for personal, non-commercial use.
- Render Free services can sleep, restart, and have no persistent disk.
- Neon Free has storage, compute, and transfer limits.
- Do not store uploads or SQLite data on Render; application data belongs in Neon.

# Website Hosting Runbook

This frontend is a Vite React single-page app hosted on Firebase Hosting.

## Local Verification

Run these before deploying:

```bash
npm ci
npm run lint
npm test
npm run build
```

The production build output is `dist/`.

## Manual First Deploy

Use this once Firebase Hosting has been enabled for project `puzzle-476822`:

```bash
npm ci
npm run build
npx firebase-tools@latest login
npx firebase-tools@latest use puzzle-476822
npx firebase-tools@latest deploy --only hosting
```

## GitHub Actions Deploy

The workflow `.github/workflows/firebase-hosting.yml` deploys manually through GitHub Actions.

Required repository variables:

- `FIREBASE_PROJECT_ID`: `puzzle-476822`
- `GCP_SERVICE_ACCOUNT`: deploy service account email
- `GCP_WORKLOAD_IDENTITY_PROVIDER`: GitHub Workload Identity provider resource name

The workflow uses Google Workload Identity Federation. Do not store service-account JSON keys or Firebase tokens in GitHub.

## Custom Domain

In Firebase Console:

1. Open Hosting.
2. Click Add custom domain.
3. Enter the domain or subdomain.
4. Add the DNS records Firebase provides at your DNS host.
5. Wait for verification and SSL certificate provisioning.

Routes such as `/alpha-api` and `/alpha-sdk` work after refresh because `firebase.json` rewrites all app paths to `/index.html`.

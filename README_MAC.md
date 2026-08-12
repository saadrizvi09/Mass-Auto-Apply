# AutoApply Cloud on an Apple Silicon Mac

The macOS launchers run the multi-user cloud product locally. They use Python 3.12,
`.venv-saas`, `.env.saas.local`, and the supported `app.saas_main:app` entrypoint. They
do not load the earlier SQLite database, desktop OAuth tokens, or local browser profile.

## First launch

1. Create a hosted development Supabase project.
2. Apply `supabase/migrations/202608080001_autoapply_cloud.sql` with the Supabase CLI.
3. Copy `.env.example` to `.env.saas.local` and add the development project values.
4. Double-click `START_HERE.command`.

If macOS displays a security warning, Control-click the file, choose **Open**, and
confirm once. The equivalent Terminal commands are:

```bash
chmod +x START_HERE.command run_mac.command dev.command setup_mac.sh
./START_HERE.command
```

The setup script installs Python 3.12, Node.js, and the Supabase CLI when missing,
creates `.venv-saas`, installs the Python dependencies, builds the browser assets, and
opens <http://127.0.0.1:8000> through the normal launcher.

## Later launches

Double-click `run_mac.command`, or run:

```bash
./run_mac.command
```

For source-code auto-reload, use:

```bash
./dev.command
```

Keep the Terminal window open while using the application and press Control-C to stop
the server. Both launchers bind only to `127.0.0.1`.

## Supabase migration

From the project directory:

```bash
supabase login
supabase link --project-ref YOUR_PROJECT_REF
supabase db push --dry-run
supabase db push
supabase migration list
```

Do not run `supabase db reset --linked`; it destroys and rebuilds the linked database.

## Tests

```bash
source .venv-saas/bin/activate
python -m pytest
npm --prefix frontend run check
```

The legacy single-user application remains available only as reference code and is not
started by these launchers.

# ContosoForge frontend

The FastAPI application serves committed production assets from `dist/`.
Running the web UI does not require Node.js or internet access.

## Rebuild after changing frontend source

Install the pinned frontend toolchain once:

```powershell
cd web\frontend
npm ci
```

Create minified, content-hashed production assets:

```powershell
npm run build
```

Commit the updated `dist/` files with the source change. Check that the committed
assets match the current source with:

```powershell
npm run build:check
```

For active frontend development, use two PowerShell windows:

```powershell
# Window 1: rebuild when frontend source changes
cd web\frontend
npm run dev
```

```powershell
# Window 2: reload the Python server when backend code changes
.\scripts\run_web.ps1 -Reload
```

The build bundles React, ReactDOM, and CodeMirror locally. `index.template.html`
contains no executable inline JavaScript, and the generated page is compatible
with the application's Content Security Policy.

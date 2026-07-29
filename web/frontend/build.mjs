import {createHash} from "node:crypto";
import {existsSync, mkdirSync, readFileSync, rmSync, watch, writeFileSync} from "node:fs";
import {basename, dirname, isAbsolute, join, relative, resolve} from "node:path";
import {fileURLToPath} from "node:url";

import {build} from "esbuild";

const frontendDir = resolve(fileURLToPath(new URL(".", import.meta.url)));
const distDir = join(frontendDir, "dist");
const checkOnly = process.argv.includes("--check");
const watchMode = process.argv.includes("--watch");

const applicationSources = [
  "api-client.js",
  "components.jsx",
  "yaml-editor.jsx",
  "log-viewer.jsx",
  "data-preview.jsx",
  "database-import.jsx",
  "quick-start.jsx",
  "app.jsx",
];
const trackedSources = [
  ...applicationSources,
  "styles.css",
  "theme-init.js",
  "index.template.html",
  "package.json",
  "package-lock.json",
  "build.mjs",
];

function read(relativePath) {
  return readFileSync(join(frontendDir, relativePath), "utf8");
}

function sourceHashes() {
  return Object.fromEntries(
    [...trackedSources].sort().map((relativePath) => [
      relativePath,
      createHash("sha256").update(read(relativePath)).digest("hex"),
    ]),
  );
}

function sourceDigest(sources) {
  const hash = createHash("sha256");
  for (const [name, digest] of Object.entries(sources)) {
    hash.update(name);
    hash.update("\0");
    hash.update(digest);
    hash.update("\0");
  }
  return hash.digest("hex");
}

function outputPath(metafile, extension) {
  const output = Object.keys(metafile.outputs).find((name) => name.endsWith(extension));
  if (!output) throw new Error(`Build did not produce a ${extension} asset.`);
  const absoluteOutput = isAbsolute(output) ? output : resolve(frontendDir, output);
  return relative(distDir, absoluteOutput).replaceAll("\\", "/");
}

function virtualEntry(path, contents, loader) {
  return {
    name: `virtual-${path}`,
    setup(context) {
      context.onResolve({filter: new RegExp(`^${path}$`)}, () => ({
        path,
        namespace: "contosoforge",
      }));
      context.onLoad({filter: /.*/, namespace: "contosoforge"}, () => ({
        contents,
        loader,
        resolveDir: frontendDir,
      }));
    },
  };
}

function verifyCommittedBuild() {
  const manifestPath = join(distDir, "asset-manifest.json");
  if (!existsSync(manifestPath)) {
    throw new Error("Production assets are missing. Run npm ci && npm run build.");
  }
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  const currentSources = sourceHashes();
  const currentDigest = sourceDigest(currentSources);
  if (manifest.sourceDigest !== currentDigest) {
    throw new Error("Production assets are stale. Run npm run build and commit web/frontend/dist.");
  }
  for (const asset of Object.values(manifest.assets)) {
    if (!existsSync(join(distDir, asset))) {
      throw new Error(`Production asset is missing: ${asset}`);
    }
  }
  console.log(`Production frontend is current (${currentDigest.slice(0, 12)}).`);
}

async function buildFrontend() {
  if (dirname(distDir) !== frontendDir || basename(distDir) !== "dist") {
    throw new Error(`Refusing to replace unexpected build directory: ${distDir}`);
  }
  rmSync(distDir, {recursive: true, force: true});
  mkdirSync(join(distDir, "assets"), {recursive: true});

  const appPrelude = `
import * as React from "react";
import {createRoot} from "react-dom/client";
import CodeMirror from "codemirror";
import "codemirror/mode/yaml/yaml.js";
import "codemirror/addon/fold/foldcode.js";
import "codemirror/addon/fold/foldgutter.js";
import "codemirror/addon/fold/indent-fold.js";
import "codemirror/addon/search/search.js";
import "codemirror/addon/search/searchcursor.js";
import "codemirror/addon/search/jump-to-line.js";
import "codemirror/addon/dialog/dialog.js";
import "codemirror/addon/selection/active-line.js";
import "codemirror/addon/edit/matchbrackets.js";
const ReactDOM = {createRoot};
`;
  const appContents = [
    appPrelude,
    ...applicationSources.map((name) => `\n/* source: ${name} */\n${read(name)}`),
  ].join("\n");
  const cssContents = `
@import "codemirror/lib/codemirror.css";
@import "codemirror/addon/fold/foldgutter.css";
@import "codemirror/addon/dialog/dialog.css";
${read("styles.css")}
`;
  const sharedOptions = {
    absWorkingDir: frontendDir,
    bundle: true,
    entryNames: "assets/[name]-[hash]",
    legalComments: "none",
    minify: true,
    outdir: distDir,
    metafile: true,
  };

  const [appResult, cssResult, themeResult] = await Promise.all([
    build({
      ...sharedOptions,
      entryPoints: {app: "application.jsx"},
      plugins: [virtualEntry("application.jsx", appContents, "jsx")],
      define: {"process.env.NODE_ENV": '"production"'},
    }),
    build({
      ...sharedOptions,
      entryPoints: {app: "application.css"},
      plugins: [virtualEntry("application.css", cssContents, "css")],
    }),
    build({
      ...sharedOptions,
      entryPoints: {theme: join(frontendDir, "theme-init.js")},
    }),
  ]);

  const assets = {
    appJs: outputPath(appResult.metafile, ".js"),
    appCss: outputPath(cssResult.metafile, ".css"),
    themeJs: outputPath(themeResult.metafile, ".js"),
  };
  const index = read("index.template.html")
    .replaceAll("__APP_JS__", assets.appJs)
    .replaceAll("__APP_CSS__", assets.appCss)
    .replaceAll("__THEME_JS__", assets.themeJs);
  writeFileSync(join(distDir, "index.html"), index, "utf8");

  const sources = sourceHashes();
  const manifest = {
    version: 1,
    sourceDigest: sourceDigest(sources),
    assets,
    sources,
  };
  writeFileSync(
    join(distDir, "asset-manifest.json"),
    `${JSON.stringify(manifest, null, 2)}\n`,
    "utf8",
  );
  console.log(
    `Built ${assets.appJs}, ${assets.appCss}, and ${assets.themeJs} (${manifest.sourceDigest.slice(0, 12)}).`,
  );
}

if (checkOnly) {
  verifyCommittedBuild();
} else {
  await buildFrontend();
  if (watchMode) {
    let timer;
    for (const relativePath of trackedSources) {
      watch(join(frontendDir, relativePath), () => {
        clearTimeout(timer);
        timer = setTimeout(() => {
          buildFrontend().catch((error) => console.error(error));
        }, 100);
      });
    }
    console.log("Watching frontend sources. Press Ctrl+C to stop.");
    await new Promise(() => {});
  }
}

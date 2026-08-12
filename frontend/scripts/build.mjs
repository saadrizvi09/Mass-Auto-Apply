import { build } from "esbuild";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const frontendDirectory = resolve(dirname(fileURLToPath(import.meta.url)), "..");

await build({
  entryPoints: [resolve(frontendDirectory, "src/supabase-entry.js")],
  outfile: resolve(frontendDirectory, "../public/vendor/supabase.js"),
  bundle: true,
  charset: "utf8",
  format: "esm",
  legalComments: "none",
  minify: true,
  platform: "browser",
  sourcemap: false,
  target: ["es2022"],
  treeShaking: true,
});

import { packager } from "@electron/packager";
import path from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
const paths = await packager({
  dir: ".",
  name: "Margin",
  appBundleId: "dev.lahacks.margin",
  platform: "darwin",
  arch: process.arch,
  out: "release",
  overwrite: true,
  asar: true,
  prune: false,
  download: { cacheRoot: path.resolve("node_modules/.cache/electron") },
  ignore: [
    /^\/src($|\/)/,
    /^\/tests($|\/)/,
    /^\/scripts($|\/)/,
    /^\/node_modules($|\/)/,
    /^\/release($|\/)/,
    /tsconfig\.json$/,
    /vite\.config\.ts$/,
  ],
});
for (const folder of paths) {
  const bundle = path.join(folder, "Margin.app");
  await promisify(execFile)("/usr/bin/codesign", [
    "--force",
    "--deep",
    "--sign",
    "-",
    bundle,
  ]);
  await promisify(execFile)("/usr/bin/codesign", [
    "--verify",
    "--deep",
    bundle,
  ]);
}
console.log("Packaged and ad-hoc signed:", paths.join("\n"));

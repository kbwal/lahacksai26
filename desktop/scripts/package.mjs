import { packager } from "@electron/packager";
import path from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { readdir, readlink, unlink, symlink } from "node:fs/promises";
const workerSource = path.resolve("../dist/lahacks-mail-worker");
async function portableLinks(folder, workerRoot) {
  for (const entry of await readdir(folder, { withFileTypes: true })) {
    const file = path.join(folder, entry.name);
    if (entry.isDirectory()) await portableLinks(file, workerRoot);
    else if (entry.isSymbolicLink()) {
      const target = await readlink(file);
      if (!path.isAbsolute(target)) continue;
      const relative = path.relative(workerSource, target);
      if (relative.startsWith("..") || path.isAbsolute(relative))
        throw new Error(`Sidecar link points outside its bundle: ${file}`);
      await unlink(file);
      await symlink(path.relative(folder, path.join(workerRoot, relative)), file);
    }
  }
}
const paths = await packager({
  dir: ".",
  name: "Margin",
  appBundleId: "dev.lahacks.margin",
  platform: "darwin",
  arch: process.arch,
  out: "release",
  overwrite: true,
  asar: true,
  extraResource: [workerSource],
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
  const workerRoot = path.join(bundle, "Contents/Resources/lahacks-mail-worker");
  await portableLinks(workerRoot, workerRoot);
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
    "--strict",
    bundle,
  ]);
}
console.log("Packaged and ad-hoc signed:", paths.join("\n"));

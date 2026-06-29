#!/usr/bin/env node
/**
 * check-html-comments.mjs — guard against the "JSX-style /* ... *\/
 * comments rendered as visible text" regression.
 *
 * ``index.html`` is raw HTML, not JSX. The HTML parser has no concept
 * of ``{/* ... *\/}`` blocks — it dumps them as visible content on
 * first paint, replacing the splash with a wall of comment text.
 *
 * The proper HTML comment form is ``<!-- ... -->``. CSS-style
 * ``/* ... *\/`` comments are valid INSIDE ``<style>`` blocks (CSS
 * has its own comment syntax) but a bare ``/* ... *\/`` block
 * anywhere else in ``index.html`` is a bug.
 *
 * Regression history: commit 32bdd2f introduced the splash with
 * ``{/* ... *\/}`` doc comments because the author was thinking in
 * JSX; the resulting page showed bullet-list text instead of the
 * "Popping" wordmark. Fixed in 621d90c. This script makes the
 * regression fail the build instead of silently shipping.
 *
 * Runs as ``prebuild`` so ``npm run build`` (and ``tsc`` afterwards)
 * never produces a broken ``dist/index.html``.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const indexPath = resolve(__dirname, "..", "index.html");
const html = readFileSync(indexPath, "utf8");

// Walk the file, tracking whether we're inside a <style>...</style>
// block. Outside <style>, any "/*" token (i.e. the start of a CSS-
// style / JSX-style comment) is a hard error.
let i = 0;
let line = 1;
let inStyle = false;
const errors = [];

while (i < html.length) {
  // Track newlines for accurate error messages.
  if (html[i] === "\n") line++;

  // Tag boundary: enter / leave <style>. Match case-insensitively
  // — HTML allows <STYLE>.
  if (!inStyle && html.slice(i, i + 7).toLowerCase() === "<style>") {
    inStyle = true;
    i += 7;
    continue;
  }
  if (inStyle && html.slice(i, i + 8).toLowerCase() === "</style>") {
    inStyle = false;
    i += 8;
    continue;
  }

  // The trigger: a CSS/JSX-style comment opener outside <style>.
  if (!inStyle && html[i] === "/" && html[i + 1] === "*") {
    // Skip past closing "-->" first by finding end of comment, so we
    // can quote the offending text below.
    const end = html.indexOf("*/", i + 2);
    const snippet =
      end === -1
        ? html.slice(i, Math.min(i + 80, html.length))
        : html.slice(i, end + 2);
    errors.push(
      `index.html:${line}: stray CSS/JSX-style comment rendered as ` +
        `visible text by the HTML parser. Use <!-- ... --> instead.\n` +
        `  near: ${snippet.slice(0, 80)}${snippet.length > 80 ? "..." : ""}`
    );
    break; // one error is enough — bail before re-scanning the rest.
  }

  i++;
}

if (errors.length) {
  console.error("check-html-comments: index.html has stray /* ... */ blocks");
  for (const e of errors) console.error("  " + e);
  process.exit(1);
}

console.log("check-html-comments: OK — index.html has no stray /* ... */ blocks");

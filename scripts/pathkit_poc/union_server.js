#!/usr/bin/env node
const fs = require('fs');
const path = require('path');
const readline = require('readline');
const PathKitInit = require('pathkit-wasm/bin/pathkit.js');

function mergePaths(PathKit, input) {
  const paths = input.paths || [];
  if (!Array.isArray(paths) || paths.length === 0) {
    throw new Error('input.paths must be a non-empty array');
  }

  const created = [];
  try {
    let merged = null;
    for (const spec of paths) {
      if (!spec || typeof spec.d !== 'string' || !spec.d.trim()) {
        throw new Error('each path spec must include a non-empty d string');
      }
      const current = PathKit.FromSVGString(spec.d);
      if (!current) {
        throw new Error('failed to parse SVG path');
      }
      created.push(current);
      if (Array.isArray(spec.matrix)) {
        if (spec.matrix.length !== 9) {
          throw new Error('matrix must have 9 numbers');
        }
        current.transform(...spec.matrix);
      }
      if (merged === null) {
        merged = current.copy();
        created.push(merged);
      } else {
        merged.op(current, PathKit.PathOp.UNION);
      }
    }

    if (merged === null) {
      throw new Error('no merged path was created');
    }

    const bounds = merged.getBounds();
    const tightBounds = merged.computeTightBounds();
    return {
      d: merged.toSVGString(),
      bounds: {
        left: bounds.fLeft,
        top: bounds.fTop,
        right: bounds.fRight,
        bottom: bounds.fBottom,
      },
      tight_bounds: {
        left: tightBounds.fLeft,
        top: tightBounds.fTop,
        right: tightBounds.fRight,
        bottom: tightBounds.fBottom,
      },
    };
  } finally {
    while (created.length) {
      const item = created.pop();
      if (item && typeof item.delete === 'function') {
        item.delete();
      }
    }
  }
}

async function main() {
  const wasmPath = path.resolve(__dirname, 'node_modules/pathkit-wasm/bin/pathkit.wasm');
  const PathKit = await PathKitInit({ wasmBinary: fs.readFileSync(wasmPath) });
  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

  for await (const line of rl) {
    const trimmed = line.trim();
    if (!trimmed) {
      continue;
    }
    if (trimmed === 'quit') {
      break;
    }
    try {
      const request = JSON.parse(trimmed);
      const result = mergePaths(PathKit, request);
      process.stdout.write(`${JSON.stringify({ ok: true, result })}\n`);
    } catch (error) {
      const message = error && error.stack ? error.stack : String(error);
      process.stdout.write(`${JSON.stringify({ ok: false, error: message })}\n`);
    }
  }
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});

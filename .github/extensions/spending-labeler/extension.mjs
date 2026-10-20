// Spending labeler canvas: a thin shim around `python -m labeling serve`.
// All logic and the page live in the Python package; this only starts it and returns its URL.

import { spawn } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createCanvas, CanvasError, joinSession } from "@github/copilot-sdk/extension";

const repo = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const servers = new Map(); // data directory -> Promise<{ child, url }>
const children = new Set();

function start(dataDir) {
    return new Promise((done, fail) => {
        const child = spawn(process.env.PYTHON || "python", ["-m", "labeling", "serve", "--data-dir", dataDir], {
            cwd: repo, stdio: ["ignore", "pipe", "pipe"], windowsHide: true,
        });
        children.add(child);
        let errors = "";
        const timer = setTimeout(() => { child.kill(); fail(new CanvasError("labeler_timeout", "the labeler did not start within 3 minutes")); }, 180_000);
        child.stderr.on("data", (chunk) => { errors = (errors + chunk).slice(-2000); });
        child.stdout.once("data", (chunk) => {
            clearTimeout(timer);
            done({ child, url: String(chunk).trim().split(/\r?\n/)[0] });
        });
        child.once("exit", (code) => {
            clearTimeout(timer);
            children.delete(child);
            servers.delete(dataDir);
            fail(new CanvasError("labeler_failed", `python -m labeling serve exited ${code}: ${errors.trim()}`));
        });
    });
}

async function server(dataDir) {
    if (!dataDir) throw new CanvasError("labeler_no_data", "pass dataDir or set FINANCE_DATA to the private data directory");
    if (!servers.has(dataDir)) servers.set(dataDir, start(dataDir));
    try {
        return await servers.get(dataDir);
    } catch (error) {
        servers.delete(dataDir);
        throw error;
    }
}

const dataDirOf = (input) => input?.dataDir || process.env.FINANCE_DATA;

await joinSession({
    canvases: [
        createCanvas({
            id: "spending-labeler",
            displayName: "Spending labeler",
            description: "Keyboard-first labeler for whose spending each row is (each person or joint) and need/want categories in Wealthfolio.",
            inputSchema: {
                type: "object",
                properties: { dataDir: { type: "string", description: "Private finance data directory (defaults to FINANCE_DATA)" } },
            },
            actions: [
                {
                    name: "progress",
                    description: "Rows and dollars left to label, and labels by source and confidence.",
                    handler: async (ctx) => {
                        const { url } = await server(dataDirOf(ctx.input));
                        const [card, audit] = await Promise.all([
                            fetch(`${url}api/card`).then((r) => r.json()),
                            fetch(`${url}api/audit?source=model&max=1`).then((r) => r.json()),
                        ]);
                        return { left: card.counts, lowestConfidenceModelLabels: audit.slice(0, 20).map((r) => ({ date: r.date, amount: r.amount, spender: r.spender, confidence: r.confidence })) };
                    },
                },
            ],
            open: async (ctx) => {
                const { url } = await server(dataDirOf(ctx.input));
                return { title: "Spending labeler", url };
            },
        }),
    ],
});

process.on("exit", () => {
    for (const child of children) child.kill();
});

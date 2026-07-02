import "dotenv/config";
import express from "express";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { chat, site } from "./assistant.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const app = express();

// Render (and most hosts) sit behind a reverse proxy — trust it so req.ip is
// the real client IP from X-Forwarded-For, not the proxy's.
app.set("trust proxy", 1);

app.use(express.json({ limit: "256kb" }));
app.use(express.static(join(__dirname, "..", "public")));

const MAX_TURNS = 30;

// ── Rate limit /api/chat ────────────────────────────────────────────────────
// Every request fans out to the Gemini API (paid/quota'd), so an unthrottled
// endpoint lets one script burn the whole quota and silently take the bot down
// for real customers. Simple in-memory sliding window per IP — good enough for
// a single-instance service; restarts just reset the counters.
const RL_WINDOW_MS = 10 * 60 * 1000;   // 10 minutes
const RL_MAX = 20;                     // max chat requests per IP per window
const rlHits = new Map();              // ip -> [timestamps]

function rateLimited(ip) {
  const now = Date.now();
  const hits = (rlHits.get(ip) || []).filter((t) => now - t < RL_WINDOW_MS);
  if (hits.length >= RL_MAX) { rlHits.set(ip, hits); return true; }
  hits.push(now);
  rlHits.set(ip, hits);
  return false;
}
// Hourly sweep so the map can't grow unbounded from one-off IPs.
setInterval(() => {
  const now = Date.now();
  for (const [ip, hits] of rlHits) {
    const live = hits.filter((t) => now - t < RL_WINDOW_MS);
    if (live.length) rlHits.set(ip, live); else rlHits.delete(ip);
  }
}, 60 * 60 * 1000).unref();

// Public branding for the widget to self-configure (no rules/KB leaked).
app.get("/api/meta", (_req, res) => {
  res.json({
    name: site.meta.assistant_name_ar,
    subtitle: site.meta.subtitle,
    greeting: site.meta.greeting_ar,
    chips: site.meta.chips,
    theme: site.meta.theme,
    escalation: site.escalation,
  });
});

app.post("/api/chat", async (req, res) => {
  try {
    const ip = (req.ip || "?").toString();
    if (rateLimited(ip)) {
      return res.status(429).json({ error: "rate_limited" });
    }
    const { messages } = req.body || {};
    if (!Array.isArray(messages) || messages.length === 0) {
      return res.status(400).json({ error: "messages array required" });
    }
    let trimmed = messages
      .map((m) => ({
        role: m.role === "assistant" ? "assistant" : "user",
        content: String(m.content || "").slice(0, 4000).trim(),
      }))
      .filter((m) => m.content)      // empty parts make Gemini reject the call
      .slice(-MAX_TURNS);
    // Gemini requires the history to start on a user turn; slicing an odd
    // window (or a greeting-first history) can leave an assistant turn first.
    while (trimmed.length && trimmed[0].role === "assistant") trimmed.shift();
    if (trimmed.length === 0) {
      return res.status(400).json({ error: "no message content" });
    }
    const result = await chat(trimmed);
    res.json({ text: result.text });
  } catch (err) {
    console.error("assistant error:", err?.message || err);
    res.status(500).json({ error: "assistant_error" });
  }
});

app.get("/health", (_req, res) => res.json({ ok: true, site: site.id }));

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  if (!process.env.GEMINI_API_KEY) {
    console.warn("⚠️  GEMINI_API_KEY غير موجود — انسخ .env.example إلى .env وأضف مفتاحك.");
  }
  console.log(`✦ A2A assistant [site: ${site.id}] → http://localhost:${PORT}`);
});

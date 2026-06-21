import "dotenv/config";
import express from "express";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { chat, site } from "./assistant.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const app = express();

app.use(express.json({ limit: "256kb" }));
app.use(express.static(join(__dirname, "..", "public")));

const MAX_TURNS = 30;

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
    const { messages } = req.body || {};
    if (!Array.isArray(messages) || messages.length === 0) {
      return res.status(400).json({ error: "messages array required" });
    }
    const trimmed = messages.slice(-MAX_TURNS).map((m) => ({
      role: m.role === "assistant" ? "assistant" : "user",
      content: String(m.content || "").slice(0, 4000),
    }));
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

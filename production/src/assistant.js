import { GoogleGenAI } from "@google/genai";
import { loadSiteConfig } from "./config.js";
import { buildSystemPrompt } from "./systemPrompt.js";

// Reads GEMINI_API_KEY from the environment (loaded via dotenv in server.js).
const ai = new GoogleGenAI({ apiKey: process.env.GEMINI_API_KEY });
const MODEL = process.env.MODEL || "gemini-2.5-flash";
// Fallback chain: if the configured model is overloaded (503) or rate-limited
// (429), try the next one before giving up. Dedup keeps the configured model
// first. These are confirmed-available models with separate per-model free
// quota pools (the 2.0-* family is quota-exhausted on the shared key, and
// gemini-1.5-flash is retired/404 — both deliberately excluded).
const MODELS = [...new Set([MODEL, "gemini-2.5-flash-lite", "gemini-3.5-flash", "gemini-3.1-flash-lite"])];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Active site config + system prompt are built once and reused.
export const site = loadSiteConfig();
const SYSTEM_PROMPT = buildSystemPrompt(site);
const esc = site.escalation;
const FALLBACK = `أعتذر، ما قدرت أكمل الطلب الحين. تقدر تتواصل معنا عبر ${esc.label}: ${esc.value}.`;

// Map our history (role: 'user' | 'assistant') → Gemini contents (role: 'user' | 'model').
function toContents(messages) {
  return messages.map((m) => ({
    role: m.role === "assistant" ? "model" : "user",
    parts: [{ text: String(m.content || "") }],
  }));
}

/**
 * Run one assistant turn via Gemini.
 * @param {Array<{role:'user'|'assistant', content:string}>} messages
 * @returns {Promise<{text:string}>}
 */
export async function chat(messages) {
  const contents = toContents(messages);
  let lastErr;
  for (const model of MODELS) {
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const response = await ai.models.generateContent({
          model,
          contents,
          config: {
            systemInstruction: SYSTEM_PROMPT, // الحواجز + قاعدة المعرفة
            maxOutputTokens: 2048,
            temperature: 0.6,
          },
        });
        const text = (response.text || "").trim();
        if (text) return { text };
        break; // empty reply → try the next model
      } catch (err) {
        lastErr = err;
        const msg = String(err?.message || err);
        // 503 high-demand / 429 rate-limit / 500 are transient → retry then fall back.
        const transient = /\b(429|500|503)\b|UNAVAILABLE|RESOURCE_EXHAUSTED|overloaded|high demand/i.test(msg);
        console.error(`gemini error [${model} #${attempt}]:`, msg.slice(0, 180));
        if (transient && attempt === 0) {
          await sleep(1200);
          continue; // retry the same model once
        }
        break; // give up on this model, try the next in the chain
      }
    }
  }
  console.error("gemini all models failed:", String(lastErr?.message || lastErr).slice(0, 180));
  return { text: FALLBACK };
}

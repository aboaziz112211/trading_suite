import { GoogleGenAI } from "@google/genai";
import { loadSiteConfig } from "./config.js";
import { buildSystemPrompt } from "./systemPrompt.js";

// Reads GEMINI_API_KEY from the environment (loaded via dotenv in server.js).
const ai = new GoogleGenAI({ apiKey: process.env.GEMINI_API_KEY });
const MODEL = process.env.MODEL || "gemini-2.5-flash";

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
  try {
    const response = await ai.models.generateContent({
      model: MODEL,
      contents: toContents(messages),
      config: {
        systemInstruction: SYSTEM_PROMPT, // الحواجز + قاعدة المعرفة
        maxOutputTokens: 1500,
        temperature: 0.6,
      },
    });
    const text = (response.text || "").trim();
    return { text: text || FALLBACK };
  } catch (err) {
    console.error("gemini error:", err?.message || err);
    return { text: FALLBACK };
  }
}

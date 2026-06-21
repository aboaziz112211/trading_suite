# A2A — المساعد الذكي (منصّة متعددة المواقع · Config-driven)

محرّك مساعد ذكي **واحد** مربوط بـ **Gemini API**، يخدم **أي موقع** بمجرد تبديل ملف إعداد — هذا جوهر "ابنِ مرة، بِع كثير".

| الموقع النشط | الملف | الوصف |
|---|---|---|
| **ChartEdge** (افتراضي) | `sites/chartedge.json` | منصة ذكاء سوقي للأسهم — يرد على استفسارات الزوّار (موقعكم = demo حي) |
| **بيت التحميص** | `sites/roasting_house.json` | متجر قهوة مختصة — يرشّح حسب الذوق (أول عميل) |

## التشغيل
```bash
cd production
npm install
cp .env.example .env          # أضف GEMINI_API_KEY، واختر SITE=chartedge أو roasting_house
npm start                     # → http://localhost:3000
```
> 🔑 المفتاح **مجاني** من Google AI Studio: https://aistudio.google.com/app/apikey — محمي بـ `.gitignore`.

## تبديل الموقع
```bash
SITE=chartedge npm start        # موقع ChartEdge
SITE=roasting_house npm start   # متجر بيت التحميص
```
الودجة **تتهيّأ ذاتياً** من `/api/meta` (الاسم، الترحيب، الأمثلة، الألوان) — نفس الملف يخدم الموقعين.

## البنية
```
sites/<id>.json     ← هوية + شخصية + قواعد (guardrails) + قاعدة معرفة/منتجات لكل موقع
src/config.js       ← يحمّل الموقع النشط (SITE)
src/systemPrompt.js ← يبني الـ system prompt العام من الإعداد
src/assistant.js    ← نداء Gemini (gemini-2.5-flash) + systemInstruction
src/server.js       ← Express: POST /api/chat + GET /api/meta
public/index.html   ← ودجة عامة تتهيّأ من /api/meta
```

## الحواجز (Guardrails)
- ❌ لا يخترع حقائق — يلتزم بقاعدة معرفة الموقع فقط.
- 🌐 يرد بلغة الزائر (عربي/إنجليزي).
- 🛡️ يتجاهل حقن التعليمات (prompt injection).
- 🧭 يبقى في نطاق الموقع، ويحوّل للتواصل البشري عند الحاجة.
- 💹 **ChartEdge:** ممنوع منعاً باتاً تقديم نصائح استثمارية/توصيات أسهم — يشرح المنصّة فقط، مع إخلاء مسؤولية "تعليمي فقط".

## كيف نضيف موقعاً جديداً (لأي عميل)؟
1. أنشئ `sites/<عميل>.json` (انسخ أحد الموجودين وعدّل الهوية/المعرفة/القواعد).
2. `SITE=<عميل> npm start`. خلاص. 🎯

## النشر والتركيب
- ارفع على **Render / Vercel** (نفس استضافة موقعكم الحالي). اضبط `GEMINI_API_KEY` و`SITE` في متغيرات البيئة.
- **دمج الفقاعة في موقعك بسطر واحد** → دليل كامل خطوة بخطوة في **[INTEGRATION.md](INTEGRATION.md)**.
- صفحة اختبار كاملة على `/` · محتوى الـ iframe على `/embed.html` · سكربت الحقن على `/widget.js`.

## الخطوات القادمة
- بثّ مباشر (streaming) + بطاقات منتجات/روابط.
- ودجة bubble قابلة للحقن + CORS لنطاق العميل.
- مزامنة تلقائية لقاعدة المعرفة (كتالوج سلة / صفحات الموقع).
- لوحة تحليلات (أكثر الأسئلة، التحويل) — باقة Pro.

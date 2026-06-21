# دمج المساعد في موقع ChartEdge (خطوة بخطوة)

الفكرة: **خدمة خلفية مستضافة** (تحمل مفتاح Gemini بأمان) + **فقاعة محادثة** تُحقن في موقعك بسطر واحد.
الفقاعة تفتح iframe يحمّل المحادثة من خادم المساعد → الطلبات same-origin → **بدون CORS وبدون تعارض تصميم**.

```
موقع ChartEdge  ──<script widget.js>──►  فقاعة 💬  ──iframe──►  خادم المساعد (Render)  ──►  Gemini API
   (موقعك)                                                        (يحمل المفتاح السري)
```

---

## الخطوة 0 — احصل على مفتاح Gemini (مجاني)
افتح **Google AI Studio** → https://aistudio.google.com/app/apikey → **Create API key** → انسخه. (طبقة مجانية سخية.)

## الخطوة 1 — ارفع الكود على GitHub
ادفع مجلد المشروع (مع `production/`) إلى مستودع GitHub. **تأكد أن `.env` غير مرفوع** (محمي بـ `.gitignore`).

## الخطوة 2 — انشر الخدمة على Render
1. Render → **New → Blueprint** → اربط المستودع (يلتقط `production/render.yaml` تلقائياً).
   - أو يدوياً: **New → Web Service** · Root Directory: `production` · Build: `npm install` · Start: `npm start`.
2. في **Environment** أضف:
   | المفتاح | القيمة |
   |---|---|
   | `GEMINI_API_KEY` | مفتاحك المجاني من Google AI Studio (سرّي) |
   | `SITE` | `chartedge` |
   | `MODEL` | `gemini-2.5-flash` |
3. انشر. راح تحصل رابطاً مثل: `https://a2a-assistant.onrender.com`
4. تأكد إنه يشتغل: افتح `https://a2a-assistant.onrender.com/health` → لازم يرجّع `{ "ok": true, "site": "chartedge" }`.

## الخطوة 3 — احقن الفقاعة في موقع ChartEdge
أضف هذا السطر **قبل `</body>`** في قالب موقعك الأساسي:

```html
<script src="https://a2a-assistant.onrender.com/widget.js" data-accent="#16c784" data-position="right" defer></script>
```
- بدّل الرابط برابط خدمتك من الخطوة 2.
- `data-accent` = لون الفقاعة (أخضر ChartEdge). `data-position` = `right` أو `left`.

## الخطوة 4 — جرّب
افتح موقع ChartEdge → لازم تظهر فقاعة 💬 أسفل الزاوية → اضغطها → اسأل "وش يقدّم ChartEdge؟".

---

## ملاحظات
- **الخطة المجانية في Render** تنام بعد خمول (~30-50ث بداية باردة). للإنتاج: خطة مدفوعة أو خدمة keep-alive.
- **لتجربة بيت التحميص** بنفس الخدمة: غيّر `SITE=roasting_house` وأعد النشر (أو انشر خدمة ثانية) — الودجة تتبدّل هويتها ذاتياً.
- **الأمان:** المفتاح يبقى في خادم Render فقط، ما يوصل المتصفح أبداً. الـ iframe يعزل التصميم عن موقعك.
- **لا حاجة لـ CORS** في هذا الإعداد. (لو لاحقاً تبي تنادي `/api/chat` مباشرة من سكربت في موقعك بدون iframe، عندها تحتاج تفعّل CORS للنطاق.)

## استكشاف الأخطاء
| المشكلة | الحل |
|---|---|
| الفقاعة ما تظهر | تأكد إن السطر قبل `</body>` وإن الرابط صحيح (افتح `/widget.js` في المتصفح). |
| تفتح بس ما ترد | افتح `/health`؛ لو الردود فاضية → تأكد من `GEMINI_API_KEY` في Render وإن المفتاح فعّال. |
| خطأ موديل غير موجود | جرّب `MODEL=gemini-2.5-flash` أو `gemini-3.1-flash-lite` (حسب توفّره على مفتاحك). |
| ردود إنجليزية فقط | عادي — يرد بلغة الزائر؛ اكتب بالعربي يرد عربي. |

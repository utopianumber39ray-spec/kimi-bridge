# Kimi Playground Bridge

## الفكرة

جسر محلي (OpenAI-compatible) يجعل نموذج **Moonshot Kimi K3** المتاح في
[NVIDIA Build Playground](https://build.nvidia.com/moonshotai/kimi-k3_1-pro/playground)
قابلًا للاستخدام من أي عميل يتكلم بروتوكول OpenAI — دون API key رسمي.

يعمل البرنامج على مبدأ واحد: يفتح Chromium في الخلفية على صفحة Playground المسجّل
دخولها، ثم ينفّذ كل طلب **داخل الصفحة نفسها** عبر CDP (Chrome DevTools Protocol)،
ويحوّل SSE الناتج إلى صيغة OpenAI.

- **Base URL:** `http://127.0.0.1:8120/v1`
- **Model id:** `moonshotai/kimi-k3.1-pro`
- **API key:** أي قيمة، مثل `anonymous` (محلي فقط)
- **CDP:** `http://127.0.0.1:9333`

## البدء السريع

```sh
git clone https://github.com/utopianumber39ray-spec/kimi-bridge.git
cd kimi-bridge
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# سجّل الدخول إلى NVIDIA مرة واحدة في متصفح profile المشروع (انظر INSTALL.md)
PYTHON=$PWD/.venv/bin/python ./install.sh --enable-watchdog
curl http://127.0.0.1:8120/v1/health      # {"ok": true}
```

**الدليل الكامل خطوة بخطوة — من تثبيت المتطلبات حتى أول استدعاء ناجح وإصلاح الأعطال — موجود في
[INSTALL.md](INSTALL.md).** اقرأه أولًا إن كانت هذه أول مرة تشغّل فيها المشروع.

## المكوّنات

| الملف | الوظيفة |
|---|---|
| `kimi-playground-bridge.py` | الخادم نفسه على `8120`: OpenAI `/v1/chat/completions` + `/v1/health`، بث SSE و tool calls |
| `cdp.py` | اتصال CDP بسيط بالصفحة، ويحترم `KIMI_CDP` |
| `nv-browser-harden.py` | تهيئة Chromium: stealth، UA، منطقة زمنية، استعادة الكوكيز، فتح Playground |
| `nv-browser-guard.py` | يبقي overrides حيّة ويحرّك المؤشر/التمرير بشكل بشري (اختياري) |
| `nv-cookie-snapshot.py` | يلتقط كوكيز NVIDIA إلى `/home/ai/.dsh/nvidia-session-cookies.json` (سرّي، 0600) |
| `nv-cookie-restore.py` | بديل/مساعد لاستعادة الكوكيز في متصفح جديد |
| `kimi-bridge-watchdog.py` | فحص دوري: إن سقط المتصفح/الصفحة/الجسر يعيد تشغيلهما بالترتيب الصحيح |
| `kimi-code-watchdog.py` | استئناف جلسات Kimi Code CLI التي انقطعت وسط الدور (اختياري) |
| `searxng-mcp.py` | MCP محلي للبحث عبر SearXNG (اختياري، `127.0.0.1:8888`) |
| `verify_local.py` | فحص الصحة + CDP + وجود صفحة Playground دون إعادة تشغيل |
| `kimi_verify.py` | اختبار شامل للأدوات والاستدلال (يرسل طلبات حقيقية) |
| `install.sh` | يولّد وحدات systemd user ويشغّل الخدمة |
| `systemd/` | قوالب الوحدات (`@ROOT@`, `@PYTHON@`, `@HOME@`, `@UID@`) |

## التشغيل اليومي

```sh
systemctl --user status kimi-playground nv-playground-browser kimi-bridge-watchdog.timer
journalctl --user -u kimi-playground -n 50 --no-pager
python3 verify_local.py
```

المتصفح هو **مصدر النموذج**، لا تغلقه يدويًا؛ أعد تشغيل الخدمات أو استخدم
`kimi-bridge-watchdog.py`. التفاصيل في [INSTALL.md](INSTALL.md#إدارة-الخدمة).

# دليل التثبيت والتشغيل الكامل

هذا الدليل يشرح كل خطوة من الصفر: المتطلبات، تسجيل الدخول إلى NVIDIA، حفظ الجلسة،
تثبيت الخدمة، التحقق، ربط أي عميل OpenAI، الأعطال الشائعة، والتحديث/الإزالة.

## 1. المتطلبات

| المتطلب | التفاصيل |
|---|---|
| نظام | Linux مع `systemd --user` (بدون sudo) |
| Chromium | إصدار 156 أو أحدث، والمسار الافتراضي `/usr/lib/chromium/chromium` |
| Python | 3.10+ (اختُبر على 3.11) |
| حزم Python | `websocket-client` و`requests` (موجودة في `requirements.txt`) |
| حساب | حساب NVIDIA مسجّل الدخول في المتصفح، مع وصول إلى صفحة Kimi K3 |

إذا كان مسار Chromium مختلفًا على جهازك، عدّل السطر `ExecStart=` في
`systemd/nv-playground-browser.service` قبل التثبيت.

## 2. جلب المشروع

```sh
git clone https://github.com/utopianumber39ray-spec/kimi-bridge.git
cd kimi-bridge
```

## 3. تجهيز بيئة بايثون

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

بديل بدون venv:

```sh
python3 -m pip install --user websocket-client requests
```

## 4. تسجيل الدخول إلى NVIDIA وحفظ الجلسة

هذه أهم خطوة، وتُفعل **مرة واحدة** (أو عند انتهاء جلسة NVIDIA).

**4.1** شغّل Chromium بواجهة مرئية على profile المشروع نفسه، مع منفذ CDP:

```sh
mkdir -p "$HOME/.cache/nvprofile"
/usr/lib/chromium/chromium \
  --remote-debugging-port=9333 \
  --remote-allow-origins='*' \
  --user-data-dir="$HOME/.cache/nvprofile" \
  "https://build.nvidia.com/moonshotai/kimi-k3_1-pro/playground"
```

**4.2** سجّل الدخول في النافذة، وتأكد أن صفحة Playground مفتوحة وأن صندوق المحادثة يعمل.

**4.3** من طرفية ثانية، احفظ كوكيز الجلسة (تشمل كوكيز الجلسة المؤقتة التي تُحذف عند
إغلاق المتصفح). الملف الناتج سرّي بصلاحية `0600`:

```sh
.venv/bin/python nv-cookie-snapshot.py
# saved N cookies (...) -> /home/ai/.dsh/nvidia-session-cookies.json
```

يمكن تغيير مسار الملف عبر `NV_COOKIE_FILE=/path/to/cookies.json`.

**4.4** أغلق نافذة Chromium التي فتحتها في 4.1 (هذه المرة فقط، لأن الخدمة ستفتح
المتصفح بنفس الـprofile في الخلفية).

## 5. تثبيت الخدمة

```sh
PYTHON="$PWD/.venv/bin/python" ./install.sh --enable-watchdog
```

يفعل `install.sh` الآتي:

1. يتحقق من وجود `websocket-client` و`requests`.
2. ينسخ قوالب `systemd/` إلى `~/.config/systemd/user/` بعد استبدال
   `@ROOT@` و`@PYTHON@` و`@HOME@` و`@UID@` بقيم جهازك.
3. ينفّذ `daemon-reload` ويفعّل `kimi-playground` و`nv-playground-browser`.
4. مع `--enable-watchdog` يفعّل `kimi-bridge-watchdog.timer` (فحص كل دقيقتين).

ثم ابدأ المتصفح والجسر:

```sh
systemctl --user start nv-playground-browser.service
systemctl --user start kimi-playground.service
```

## 6. التحقق

```sh
curl http://127.0.0.1:8120/v1/health
# {"ok": true}

.venv/bin/python verify_local.py
# bridge: OK
# browser: OK — existing Playground page found
```

فحص CDP يدويًا:

```sh
curl -s http://127.0.0.1:9333/json/list | grep -o '"url": "[^"]*playground[^"]*"'
```

## 7. أول استدعاء

استدعاء عادي:

```sh
curl http://127.0.0.1:8120/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"moonshotai/kimi-k3.1-pro","messages":[{"role":"user","content":"قل مرحبا"}],"stream":false}'
```

استدعاء متدفق (SSE بصيغة OpenAI):

```sh
curl -N http://127.0.0.1:8120/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"moonshotai/kimi-k3.1-pro","messages":[{"role":"user","content":"اكتب جملة قصيرة"}],"stream":true}'
```

إعداد أي عميل متوافق مع OpenAI:

```yaml
base_url: http://127.0.0.1:8120/v1
api_key: anonymous
model: moonshotai/kimi-k3.1-pro
```

مثال في `opencode.json`:

```json
{
  "provider": {
    "kimi-bridge": {
      "npm": "@ai-sdk/openai-compatible",
      "options": { "baseURL": "http://127.0.0.1:8120/v1", "apiKey": "anonymous" },
      "models": { "moonshotai/kimi-k3.1-pro": {} }
    }
  }
}
```

## 8. التشغيل بدون systemd

إذا كان لديك متصفح يعمل بالفعل على CDP 9333:

```sh
KIMI_CDP=http://127.0.0.1:9333 .venv/bin/python kimi-playground-bridge.py
```

لتشغيل نسخة ثانية على منفذ آخر مع نفس الجلسة:

```sh
KIMI_BRIDGE_PORT=8121 KIMI_CDP=http://127.0.0.1:9333 .venv/bin/python kimi-playground-bridge.py
```

## 9. إدارة الخدمة

```sh
systemctl --user status kimi-playground nv-playground-browser kimi-bridge-watchdog.timer
journalctl --user -u kimi-playground -n 50 --no-pager
systemctl --user restart kimi-playground
```

المراقب `kimi-bridge-watchdog.py` يفحص الصحة ووجود صفحة Playground، وإن فشل أحدها
يعيد تشغيل المتصفح أولًا (لإعادة إنشاء الصفحة المسجّلة) ثم الجسر. تشغيله يدويًا:

```sh
.venv/bin/python kimi-bridge-watchdog.py
```

> المتصفح الخلفي هو مصدر النموذج: لا تغلقه يدويًا. استخدم إعادة تشغيل الخدمات أو
> المراقب، وإلا ينقطع الجسر حتى يُعاد فتح الصفحة.

## 10. متغيرات البيئة

كلها اختيارية وافتراضاتها تكفي للاستخدام العادي:

| المتغير | الافتراضي | المعنى |
|---|---|---|
| `KIMI_BRIDGE_PORT` | `8120` | منفذ الخادم المحلي |
| `KIMI_CDP` | `http://127.0.0.1:9333` | عنوان CDP للمتصفح |
| `KIMI_MODEL` | `moonshotai/kimi-k3.1-pro` | معرّف النموذج المعروض للعملاء |
| `KIMI_UPSTREAM` | نقطة نهاية Playground | عنوان التنفيذ داخل الصفحة |
| `KIMI_FUNCTION_ID` | معرّف وظيفة Playground | يُستخدم في مسار الطلب |
| `KIMI_SITEKEY` | مفتاح hCaptcha للصفحة | مفتاح widget |
| `KIMI_TIMEOUT` | `900` | أقصى مدة طلب (ثانية) |
| `KIMI_IDLE_TIMEOUT` | `120` | أقصى خمول في البث (ثانية) |
| `KIMI_MAX_STREAM_MB` | `8` | حد حجم البث (يمنع استجابات التفكير الجامحة) |
| `KIMI_CONCURRENCY` | `3` | عدد طلبات NVIDIA المتزامنة (مطابق لمخزون التوكنز `KIMI_TOKEN_POOL`) |
| `KIMI_DEFAULT_EFFORT` | `none` | `reasoning_effort` الافتراضي |
| `KIMI_USE_MAX_OUTPUT` | `1` | رفع سقف الإخراج إلى 65536 |
| `KIMI_TOKEN_POOL` | `3` | حجم مخزون توكن hCaptcha |
| `KIMI_TOKEN_TTL` | `100` | عمر التوكن (ثانية) |
| `KIMI_MIN_GAP`/`KIMI_JITTER` | `0.5`/`0.8` | تباعد الطلبات |
| `KIMI_MAX_IMAGES` | `5` | حد الصور في الطلب |
| `KIMI_DUMP_DIR` | فارغ (معطّل) | حفظ دامبات تشخيصية |
| `NV_COOKIE_FILE` | `/home/ai/.dsh/nvidia-session-cookies.json` | ملف الكوكيز |
| `NV_TZ` | `Asia/Baghdad` | منطقة المتصفح الزمنية |

لا تفعّل `KIMI_PREMINT=1`: توليد التوكن مسبقًا يحرق سمعة الجلسة لدى NVIDIA.

## 11. ميزانية الإخراج

Playground يحسب التفكير المخفي داخل نفس `max_tokens`. لذلك يرفع الجسر السقف إلى
`65536` عندما لا يحدد العميل قيمة، ويحوّل `max` إلى `high`. هذا سقف لا إجبار:
النموذج يتوقف عند انتهاء الإجابة، لكن يبقى له متسع بعد التفكير بدل أن ينتهي
`finish_reason=length` برد فارغ.

## 12. الأعطال الشائعة

| العرض | السبب | الحل |
|---|---|---|
| `Connection refused` على 8120 | الجسر متوقف | `systemctl --user restart kimi-playground` |
| `{"ok": false}` أو صفحة Playground غير موجودة | المتصفح/الصفحة سقطت | `systemctl --user restart nv-playground-browser` ثم أعد تشغيل الجسر، أو شغّل المراقب |
| المتصفح يبقى `activating` | `--user-agent` غير مقتبس في الوحدة (Chromium 156+ يرفض `headless`) | أبقِ الـUA بين علامتي اقتباس كما في القالب |
| `incomplete_upstream_stream` | NVIDIA ينهي البث مبكرًا عند سياق ضخم أو ازدحام | جلسة/سياق أقصر أو effort أخف |
| `hCaptcha token rejected by NVIDIA` | widget أعاد توكنًا مستهلكًا | الجسر يعمل `reset()` ويعيد التوليد تلقائيًا؛ تأكد أن الصفحة مسجّلة الدخول |
| `Token is invalid` ثم `mint failed` | جلسة NVIDIA انتهت/تسجيل خروج | سجّل الدخول من جديد (خطوة 4) ثم أعد تشغيل المتصفح |
| الرد فارغ مع `finish_reason=length` | التفكير استهلك كل السقف | ارفع `max_tokens` أو خفّض effort |
| فشل توليد التوكن بعد إعادة تشغيل المتصفح | snapshot قديم/غير موجود | أعد تنفيذ `nv-cookie-snapshot.py` بعد تسجيل الدخول |

## 13. التحديث والإزالة

التحديث:

```sh
git pull
.venv/bin/pip install -r requirements.txt
PYTHON="$PWD/.venv/bin/python" ./install.sh --enable-watchdog
systemctl --user restart nv-playground-browser kimi-playground
```

الإزالة:

```sh
systemctl --user disable --now kimi-playground.service nv-playground-browser.service kimi-bridge-watchdog.timer
rm -f ~/.config/systemd/user/{kimi-playground,nv-playground-browser,kimi-bridge-watchdog}.service \
      ~/.config/systemd/user/kimi-bridge-watchdog.timer
systemctl --user daemon-reload
```

ملف الكوكيز والـprofile يبقيان افتراضيًا. لحذفهما:

```sh
rm -f /home/ai/.dsh/nvidia-session-cookies.json
rm -rf "$HOME/.cache/nvprofile"
```

## 14. الأمان

- المستودع لا يحتوي أي كوكيز أو كلمات مرور أو جلسات.
- `nv-cookie-snapshot.py` يكتب خارج المشروع بصلاحية `0600`، و`.gitignore` يمنع
  إضافة أي ملف كوكيز أو `.env` أو مفاتيح.
- لا تشارك ملف الكوكيز ولا ترفعه؛ من يملكه يملك جلستك.
- الخادم يستمع على `127.0.0.1` فقط ولا يعرض نفسه للشبكة.

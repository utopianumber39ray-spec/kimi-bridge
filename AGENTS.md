# تعليمات الوكيل — تشغيل Kimi Playground Bridge

> اقرأ هذا الملف قبل تعديل أي شيء. المجلد مستقل ويعمل على **هذا الجهاز** حاليًا،
> وليس نسخة أسرار أو كوكيز. المستخدم يريد الحفاظ على جلسة NVIDIA الحالية وعدم
> إعادة تسجيل الدخول عند كل restart.

## الفكرة باختصار

`kimi-playground-bridge.py` خادم OpenAI-compatible على `8120`. يتصل عبر CDP بصفحة NVIDIA المفتوحة في متصفح headless. يستعمل cookies الجلسة من الصفحة وينتج token captcha داخلها لكل طلب ثم يحوّل SSE إلى OpenAI SSE. لا يوجد هنا API رسمي لـNVIDIA.

- Bridge: `http://127.0.0.1:8120/v1/chat/completions`
- Health: `http://127.0.0.1:8120/v1/health`
- Model id: `moonshotai/kimi-k3.1-pro`
- Browser CDP: `http://127.0.0.1:9333`
- Playground: `https://build.nvidia.com/moonshotai/kimi-k3_1-pro/playground`

## أين الجلسة؟

**ليس الحساب نفسه؛ الجلسة = كوكيز تسجيل الدخول + صفحة Playground المفتوحة.**

- Profile المتصفح الدائم: `/home/ai/.cache/nvprofile`
- CDP الحي: `http://127.0.0.1:9333` (خدمة `nv-playground-browser.service`)
- كوكيز NVIDIA المحفوظة: `/home/ai/.dsh/nvidia-session-cookies.json` (600، لا تنسخها ولا تعرضها).
- المتصفح **headless في الخلفية**، ليس GUI.
- إعادة تشغيل البروكسي فقط لا تعمل refresh للصفحة. إعادة المتصفح تعيد فتح صفحة Playground في الخلفية وتعيد cookie snapshot؛ **عادة نفس تسجيل الدخول** يبقى، لا حساب جديد.
- إعادة استخدام نفس ملف profile من مكان آخر على الجهاز لا يصنع حسابًا أو جلسة جديدة. انتهاء الكوكيز/تسجيل الخروج يتطلب تدخل المستخدم لتسجيل الدخول؛ لا تطلب كلمات مرور.

## تشغيل آمن بجلسة قائمة (أول خيار)

إذا كانت خدمات `nv-playground-browser` و`kimi-playground` active، لا تعِد تشغيل المتصفح ولا تنشئ profile جديدًا. افحص أولًا:

```sh
cd /home/ai/kimi-bridge
python3 verify_local.py
# استخدم مفسّر يحتوي websocket-client وrequests عند الحاجة:
/home/ai/anu_ai/.venv/bin/python verify_local.py
```

- `verify_local.py` يفحص الصحة وCDP وصفحة Playground بدون restart.
- `kimi-bridge-watchdog.py` يفحص الصحة، وإن فشل يعيد المتصفح والصفحة ثم الجسر.
- توصيل DSH: provider OpenAI-compatible `baseURL=http://127.0.0.1:8120/v1`، والنموذج `moonshotai/kimi-k3.1-pro`. إذا كان مطلوب API key استخدم `anonymous` (غير مهم محليًا).
- لا تحتاج إلى `install.sh` للتشغيل الحالي: الخدمة مثبتة بالفعل.
- لتشغيل مباشر بدون systemd (متصفح قائم على 9333):

```sh
KIMI_CDP=http://127.0.0.1:9333 \
  /home/ai/anu_ai/.venv/bin/python /home/ai/kimi-bridge/kimi-playground-bridge.py
```

للتشغيل بمنفذ مختلف مع **نفس** جلسة CDP 9333: `KIMI_BRIDGE_PORT=8121 KIMI_CDP=http://127.0.0.1:9333` ثم شغّل البرنامج. لا تعمل `install.sh` عندها لأنه يستبدل الخدمة المشتركة على 8120. طلبات كل عميل منفصلة؛ الحساب وسعة NVIDIA مشتركان.

## تشغيل خدمة جديدة عند عدم وجود CDP

افحص `curl http://127.0.0.1:9333/json/list`. إذا لم يعمل:

```sh
cd /home/ai/kimi-bridge
./install.sh --enable-watchdog
systemctl --user restart nv-playground-browser.service
systemctl --user restart kimi-playground.service
```

خدمة المتصفح تستخدم `/home/ai/.cache/nvprofile` نفسه. الكوكيز في `/home/ai/.dsh/nvidia-session-cookies.json` (سرية، 0600) ولا توجد داخل الحزمة.

## المراقبة والفحص

```sh
/home/ai/anu_ai/.venv/bin/python verify_local.py
systemctl --user status kimi-bridge-watchdog.timer
journalctl --user -u kimi-bridge-watchdog.service -n 30 --no-pager
```

المراقب `kimi-bridge-watchdog.timer` كل دقيقتين. `kimi-playground.service` فيه `Restart=always` و `RestartSec=5`. هو لا يستطيع تجاوز انتهاء جلسة NVIDIA أو CAPTCHA مرئي/تسجيل دخول مطلوب.

## ملفات الحزمة

- `kimi-playground-bridge.py`: السيرفر الفعلي، port 8120، OpenAI-compatible stream/tool-calls.
- `cdp.py`: اتصال CDP للصفحة، ويحترم `KIMI_CDP`.
- `nv-browser-harden.py`: تهيئة المتصفح، استعادة كوكيز، فتح Playground.
- `nv-browser-guard.py`: focus/viewport حي للخلفية.
- `nv-cookie-snapshot.py`: يلتقط كوكيز NVIDIA الحساسة إلى `/home/ai/.dsh/nvidia-session-cookies.json` فقط؛ لا تنسخه للحزمة ولا تشاركه.
- `kimi-bridge-watchdog.py`: يفحص الصحة ويعيد المتصفح والجسر إن لزم.
- `verify_local.py`: فحص CDP/page/health بدون إعادة تشغيل.
- `kimi_verify.py`: اختبار أدوات كامل؛ يرسل طلبات استدلال حقيقية فلا تشغله عشوائيًا أثناء عمل المستخدم.
- `install.sh`: يثبت وحدات systemd للحزمة؛ لا يكتب كلمات مرور أو كوكيز. الخيارات: `--attach-existing`، `--enable-watchdog`.
- `systemd/`: قوالب الوحدات (`kimi-playground`، `nv-playground-browser`، `kimi-bridge-watchdog`).

## أعطال شائعة

- متصفح عالق في `activating` + `Multiple targets are not supported in headless mode`: سببها `--user-agent` غير المقتبس في Chromium 156+ (systemd يفكّك المسافات إلى وسائط). أبقِ الـUA بين علامتي اقتباس؛ الوحدة فيها `TimeoutStartSec=45` كي لا تعلق.
- `500 bridge_error` / `Connection refused`: البروكسي متوقف؛ شغّل `kimi-playground` وتأكد من صفحة Playground على CDP 9333.
- `Stream ended without finish_reason` ما زال يظهر في DSH رغم أن البث مكتمل: سببها NVIDIA يُنهي البث مبكرًا في التفكير الطويل؛ البروكسي صار يكتشف الإغلاق بلا `finish_reason` ويُبلّغ بخطأ `incomplete_upstream_stream` صراحةً. السبب الفعلي: سياق ضخم (~400k tokens) أو ازدحام. العلاج: جلسة جديدة، أو سياق أقصر، أو effort أخف؛ ليس عطل في البروكسي.
- `hCaptcha token rejected by NVIDIA`: كان سببه أن widget hCaptcha يعيد توكنًا محلولًا مسبقًا عند `execute()` المتكرر، فيصل توكن مستهلك إلى NVIDIA. الحل المطبّق: `hcaptcha.reset()` قبل كل `execute()`، وعند تكرار الرفض تُحدَّث الصفحة (`Page.navigate`) ويُعاد التوليد. لا تعِد تفعيل pre-mint (`KIMI_PREMINT=1`) لأنه يحرق سمعة الجلسة.


## قيود مهمة
- **لا تنهِ جلسة المتصفح أبدًا (الخلفية `headless=new` على CDP 9333 / profile `~/.cache/nvprofile`/)** عندما يكون الجسر يعمل: هذه الصفحة NVIDIA Playground هي مصدر النموذج الذي يشغّل Kimi — إغلاقها = تعطيل الوكيل نفسه. إن ظهرت مشكلة، استخدم `kimi-bridge-watchdog` / `nv-browser-guard` / إعادة تشغيل `nv-playground-browser` و`kimi-playground` — **لا تغلق المتصفح يدويًا**.
- **نفذ المهمة كاملة من أولها لآخرها بدون أي سؤال**: لا تتوقف لطلب إذن، ولا تطرح أي سؤال استشاري، ولا تعيد أي اختيار للمستخدم. إن اختفى الحل المعتاد، اختر الحل المعقول واكمل، وتسجيل ما فعلته في التقرير النهائي.

## ملاحظات
- Kimi Code Web و`kimi-code-web`/`kimi-code-tunnel` مُعادت لتثبيتها بناءً على طلب المستخدم (واجهة على 58627 وتكشف عبر Cloudflare مع bearer auth مفعّل ونفق غير محلول). لا تغلقها إلا بطلب صريح.
- **السقف** output 65536 (لا يجبر النموذج على توليدها كلها): يسمح بالتفكير ثم ترك مساحة للجواب/الأدوات. `max` maps to high to reduce endless reasoning.

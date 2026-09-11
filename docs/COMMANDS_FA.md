# دستورهای روزمره

## اعتبار هر جهت و توضیحات چارت

در گزارش‌های جدید، وضعیت نمونهٔ Long و Short جداست: `INSUFFICIENT` یعنی کمتر از
۳۰ معاملهٔ بسته‌شده، `MISSING` یعنی اطلاعات کافی ثبت نشده، `DISABLED` یعنی آن جهت
خاموش است و `SUFFICIENT` یعنی کف تعداد نمونه تأمین شده است. این کف یک قاعدهٔ
غربال پژوهشی است، نه احتمال موفقیت یا اثبات سودآوری. سود خالص هر جهت جدا گزارش می‌شود.

Auto برای ACCEPT به حداقل ۳۰ معامله و سود خالص مثبت در هر جهت فعالِ Final نیاز دارد؛
نبود این شواهد، نامزد را حداکثر در WATCH نگه می‌دارد. معاملات مراحل هم‌پوشان با هم
جمع نمی‌شوند. معیارهای رد قبلی همچنان اعمال می‌شوند. حساب دو جهت مشترک است؛
این گزارش جای ارزیابی مستقل روی دادهٔ دیده‌نشده را نمی‌گیرد. تعداد معامله یا ورود
اجباری اضافه نمی‌شود و قواعد اجرای استراتژی تغییر نمی‌کنند.

در `result.json`، بخش `directional_evidence` و در Excel بخش‌های Long/Short را ببینید.
در Auto، وضعیت‌ها در `OVERVIEW.md`، جدول Hall of Fame و خلاصهٔ هر نامزد هستند.
گزارش مجدد کمپین، طبقه‌بندی قدیمی فاقد این شواهد را هم از نتایج ذخیره‌شده به‌روز می‌کند.

Reasonهای چارت Pulse اکنون اطلاعات سیگنالِ همان کندل، نتیجه و آستانهٔ فیلترها،
طرح خروج، استاپ، ریسک، حجم، اهرم و اطلاعات واقعی اجرای سفارش را نشان می‌دهند.
خروج نیز علت مشخص، تصمیم کندل قبلی در خروج‌های زمان‌بندی‌شده، سود، هزینه و مدت
معامله دارد. فیلتر خاموش صریحاً مشخص است؛ امتیاز ورود خاموش، علت ورود معرفی نمی‌شود.
برای دیدن توضیحات جدید باید بک‌تست و چارت دوباره ساخته شوند.

کمپین تازهٔ ۳۰۰ چرخه‌ای با انتقال سابقهٔ `2026_09_10` به پوشهٔ `2026-09-11`:

```powershell
python optimize.py pulse --timeframe 1m --cycles 300 --seed-campaign outputs/pulse/optimize/2026_09_10 --output-dir outputs/pulse/optimize/2026-09-11 --workers 8
```

برای ادامهٔ همین پوشه پس از توقف، از `python optimize.py resume outputs/pulse/optimize/2026-09-11 --cycles 300` استفاده کنید.
انتقال، Hall of Fame و تاریخچهٔ نمونه‌ها را می‌خواند و فقط `best_params.json` نیست.
به‌علت تغییر شبکه/کد، پیشنهادهای قبلی دوباره ارزیابی می‌شوند و امتیازهای قدیمی
به‌عنوان نتیجهٔ جدید ثبت نمی‌شوند. snapshot انتقال در مقصد ذخیره می‌شود.

Quality اکنون اهرم‌های `1, 2, 3, 5, 8, 10, 20, 30, 50, 75, 100` را مستقل
برای Long و Short جست‌وجو می‌کند. `long_leverage` و `short_leverage` بر مقدار عمومی
`leverage` اولویت دارند. اهرم بالاتر لزوماً معامله را بزرگ‌تر نمی‌کند: اندازهٔ
سفارش با حداقلِ سقف ریسک توقف، سقف exposure، تخصیص مارجین و وجه موجود تعیین می‌شود.

برای Optimize یک‌دست از `optimize.py` استفاده کنید:

```powershell
python optimize.py pulse --dry-run
python optimize.py pulse
python optimize.py ma --dry-run
```

Pulse به‌صورت پیش‌فرض 1m، پروفایل quality، چهار چرخه و قیف
`256 → 64 → 32 → WF 16×3 → 16` دارد. مجموع سقف ارزیابی‌ها در هر چرخه
از 178 به 416 افزایش یافته؛ زمان واقعی به تعداد معاملات، داده و پردازنده بستگی دارد.
این افزایش پوشش جست‌وجو است و اثبات بهبود سود نیست.

`run_pulse_optimize.py` میان‌بر همان موتور است و همین پیش‌فرض‌ها را از تنظیمات
Pulse می‌خواند؛ تفاوت اصلی، ساخت خودکار نام پوشه بر اساس داده و نسخه است.
پرچم‌های Optimize مانند `--cycles`، `--output-dir` و `--auto-tests` را هم می‌پذیرد.
نسخهٔ جدید نام پوشهٔ v3 می‌سازد تا بودجهٔ جدید با checkpoint قبلی اشتباه نشود.
Resume تنظیمات ذخیره‌شدهٔ کمپین قبلی را حفظ می‌کند.

برای ادامهٔ کمپین یا شروع کمپین تازه با تجربهٔ آن:

```powershell
python optimize.py resume outputs/pulse/optimize/transfer_new --cycles 8
python optimize.py pulse --seed-campaign outputs/pulse/optimize/2026_09_10 --output-dir outputs/pulse/optimize/new_search
```

`--cycles 8` سقف کل چرخه‌های تکمیل‌شده است؛ مثلاً از چهار چرخه به هشت می‌رسد.

برای فایل پارامتر، در هر سه مسیر فقط `--params-file FILE` کافی است:

```powershell
python pulse_strategy.py --params-file outputs/pulse/optimize/new_search/best_params.json
python ma_strategy.py --params-file outputs/ma/optimize/best_params.json
python optimize.py pulse --params-file outputs/pulse/optimize/new_search/best_params.json --output-dir outputs/pulse/optimize/another_search
```

مسیر فایل را مطابق خروجی خود جایگزین کنید. بدون این گزینه، تنظیمات پایه استفاده
می‌شود. `--params-source config|best|file` برای انتخاب صریح منبع موجود است؛
معمولاً نیازی به آن ندارید. نام‌های قدیمی `--base-params` و `--base-source`
در Optimize برای سازگاری باقی مانده‌اند. در Optimize فایل، نقطهٔ شروع و مقادیر
ثابت را تعیین می‌کند؛ متغیرهای grid همچنان تغییر می‌کنند. در بک‌تست همان پارامترها اجرا می‌شوند.

برای مقایسهٔ دقیق Pulse با Optimize از بازپخش کمپین استفاده کنید تا تاریخ و داده هم یکسان باشند:

```powershell
python pulse_strategy.py --campaign outputs/pulse/optimize/new_search
```

`run_pulse_200.py` میان‌بر قدیمی کمپین طولانی با بودجهٔ متفاوت است؛ برای کار روزمره
دستورهای بالا کافی‌اند.

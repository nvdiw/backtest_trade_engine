# اجرای ساده و ادامهٔ optimize

برای ادامهٔ یک اجرای قبلی فقط مسیر پوشه را بدهید:

```powershell
python optimize.py --resume outputs/pulse/optimize/my_run
```

اگر نام پوشه زیر `outputs` یکتا باشد، نام کوتاه هم کافی است:

```powershell
python optimize.py --resume my_run
```

strategy، profile، داده و تنظیمات جست‌وجو از اطلاعات ذخیره‌شده بازیابی می‌شوند؛ نیازی به تکرار `base-source`، `base-params` و `output-dir` نیست. نام تکراری خطا می‌دهد تا اجرای اشتباه ادامه پیدا نکند. پوشه‌های Auto قدیمی از `auto_state.json` و اجراهای دارای manifest از تنظیمات ثبت‌شده استفاده می‌کنند. اجراهای جدید تنظیمات را در `campaign_config.json` هم نگه می‌دارند، حتی وقتی audit خاموش باشد.

تعداد worker و محدودیت تعداد cycle را می‌توانید برای همین اجرا تعیین کنید:

```powershell
python optimize.py --resume my_run -w 4 --auto-cycles 10
```

معنای `auto-cycles` همان معنای قبلی optimizer است. محدودیت اجرای قبلی خودکار تکرار نمی‌شود؛ resume بدون این گزینه تا توقف دستی ادامه دارد. برای حفظ سازگاری، تنظیمات جست‌وجوی Auto از checkpoint بازیابی می‌شوند؛ برای آزمایش متفاوت پوشهٔ جدید بسازید.

شروع یک Auto جدید:

```powershell
python optimize.py --strategy pulse --auto --profile signal --output-dir outputs/pulse/optimize/my_run
```

در Auto معمولی، snapshot پیش‌فرض هر **۵۰ cycle کامل** ذخیره می‌شود. فاصلهٔ ذخیره‌شدهٔ اجرای قبلی هنگام resume حفظ می‌شود. در staged/autopilot فاصلهٔ snapshot همچنان تابع تکمیل برنامهٔ phaseهاست. اسکریپت محدود `run_pulse_optimize.py` عمداً اجرای ۴-cycle و snapshot در انتهای آن را حفظ می‌کند.

## بهترین ترکیب‌ها را کجا ببینم؟

- در ترمینال پس از هر cycle، سه ترکیب برتر با وضعیت، امتیاز، سود و افت سرمایه نمایش داده می‌شوند.
- در پوشهٔ Auto، فایل `OVERVIEW.md` پنج ترکیب برتر و مقایسهٔ مقدار پارامترهایشان را نشان می‌دهد؛ پارامترهای متفاوت جلوتر هستند.
- اولین sheet گزارش Auto و snapshot، `Quick Compare` است. `Parameter Compare` در صورت وجود grid ذخیره‌شده، مقادیر پنج ترکیب برتر را کنار هم می‌گذارد.
- فایل `best_params.json` ترکیب رتبهٔ اول است؛ وضعیت `WATCH` یا `REJECT` و علت تصمیم را هم بررسی کنید. رتبهٔ اول لزوماً به معنای عبور از معیارهای ارزیابی نیست.

رتبه‌بندی برای **ترکیب کامل پارامترها** است، نه اثبات بهترین بودن یک مقدار به‌تنهایی. گزارش‌های مفصل قبلی همچنان تولید می‌شوند.

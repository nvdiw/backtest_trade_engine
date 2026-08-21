# راهنمای فارسی Backtest Trade Engine

[English guide](README.md)

این پروژه یک موتور بک‌تست کندل‌به‌کندل بیت‌کوین و ابزار بهینه‌سازی پارامترها است. امکانات اصلی آن شامل معامله Long و Short، اهرم، کارمزد، liquidation، ورود پله‌ای، کنترل‌های ماهانه، امتیازدهی MA/EMA/ADX/ATR/Volume/RSI، چارت تعاملی، بهینه‌سازی چندپردازه، ادامه اجرای قطع‌شده و اعتبارسنجی خارج از نمونه است.

> این نرم‌افزار برای تحقیق است و نتیجه بک‌تست تضمین‌کننده عملکرد آینده نیست.

## مدل اجرای صحیح معاملات

- سیگنال کندل `i` فقط بعد از بسته‌شدن همان کندل قابل محاسبه است.
- ورود و خروج سیگنالی در `Open` کندل `i+1` اجرا می‌شود؛ بنابراین از قیمت گذشته با اطلاعات آینده استفاده نمی‌شود.
- liquidation با High/Low کندلی بررسی می‌شود که پوزیشن واقعاً در آن فعال است.
- سود خالص معاملات بسته‌شده شامل کارمزد است.
- پوزیشن باز در پایان داده با آخرین Close ارزش‌گذاری می‌شود. سود realized، سود unrealized و تعداد پوزیشن باز جداگانه در نتیجه وجود دارند.
- `--start` شامل ابتدا و `--end` بدون احتساب انتها است.

## نصب و داده موردنیاز

- Python 3.9 یا جدیدتر
- `pandas`، `numpy`، `matplotlib` و `mplfinance`
- `openpyxl` برای گزارش‌های قالب‌بندی‌شده Excel

```powershell
python -m pip install pandas numpy matplotlib mplfinance
python -m pip install openpyxl
```

مسیر پیش‌فرض داده در `fetch_calculate_data.py` تنظیم شده است:

```text
data_candle/btc_15m_data_2018_to_2026.csv
```

ستون‌های ضروری CSV:

```text
Open time, Open, High, Low, Close, Volume, Close time
```

## شروع سریع

```powershell
# اجرای عادی همراه چارت تعاملی
python ma_strategy.py

# اجرای سریع، بدون پنجره و بدون ساخت فایل گزارش
python ma_strategy.py --start 2025-01-01 --end 2026-01-01 `
  --no-chart --no-trade-log --quiet --print-result

# بهینه‌سازی هوشمند و تکرارپذیر
python optimize.py --mode smart --profile focused --tests 5000 -w 8 `
  --start 2023-01-01 --end 2025-01-01

# جست‌وجوی مرحله‌ای پیوسته؛ توقف امن با Ctrl+C
python optimize.py --auto -w 16
```

## دفترچه راهنمای `ma_strategy.py`

### تعیین محدوده با تاریخ یا شماره کندل

در حالت تاریخ، شروع محاسبه می‌شود ولی انتها محاسبه نمی‌شود:

```powershell
python ma_strategy.py --start 2025-01-01 --end 2025-06-01
```

می‌توان شماره کندل را نیز مستقیماً داد:

```powershell
python ma_strategy.py --start 100000 --end 120000
```

مقدار end باید بعد از start باشد. تبدیل تاریخ به index با timestampهای فایل دیتای تنظیم‌شده انجام می‌شود.

### تمام گزینه‌های خط فرمان

| گزینه | پیش‌فرض | کاربرد |
|---|---:|---|
| `-h`, `--help` | — | نمایش راهنمای داخلی برنامه. |
| `--start VALUE` | `2025-01-01` | تاریخ یا index شروع به‌صورت inclusive. |
| `--end VALUE` | `2026-02-23` | تاریخ یا index پایان به‌صورت exclusive. |
| `--params-source config\|best\|file` | `config` | انتخاب منبع پارامترها. |
| `--best-params FILE` | `outputs/optimize/best_params.json` | فایل برنده برای حالت `best`. |
| `--params-file FILE` | — | JSON دلخواه برای حالت `file`. |
| `--config FILE` | — | نام قدیمی معادل `--params-source file --params-file FILE`. |
| `--set NAME=VALUE` | — | تغییر موقت یک پارامتر معتبر؛ قابل تکرار است. |
| `--list-params` | — | نمایش تمام مقادیر پیش‌فرض به‌شکل JSON و خروج. |
| `--no-chart` | خاموش | باز نکردن چارت تعاملی. |
| `--save-chart FILE` | — | ذخیره چارت با فرمت PNG/PDF/SVG. |
| `--quiet` | خاموش | عدم نمایش پیام معامله‌ها و گزارش متنی. |
| `--no-trade-log` | خاموش | نساختن CSV معاملات، CSV ماهانه و Excel. |
| `--excel` | روشن | ساخت گزارش XLSX چندبرگه‌ای؛ برای سازگاری نگه داشته شده است. |
| `--no-excel` | خاموش | نساختن XLSX و نگه‌داشتن فقط خروجی‌های CSV. |
| `--output-dir DIR` | `outputs` | پوشه اصلی گزارش معاملات و گزارش ماهانه. |
| `--result-json FILE` | — | ذخیره دیکشنری نتیجه نهایی در JSON. |
| `--print-result` | خاموش | چاپ دیکشنری نتیجه نهایی به‌صورت JSON. |

برای دیدن نسخه همیشه به‌روز گزینه‌ها:

```powershell
python ma_strategy.py --help
python ma_strategy.py --list-params
```

### منابع پارامتر و ترتیب اولویت

حالت `config` از مقادیر `strategy_config.py` استفاده می‌کند و آن فایل را تغییر نمی‌دهد:

```powershell
python ma_strategy.py --params-source config
```

حالت `best` خروجی optimizer را می‌خواند:

```powershell
python ma_strategy.py --params-source best
python ma_strategy.py --params-source best `
  --best-params outputs/optimize/run_01/best_params.json
```

حالت `file` یک JSON دلخواه می‌خواند:

```powershell
python ma_strategy.py --params-source file --params-file configs/conservative.json
```

گزینه `--set` در آخر اعمال می‌شود و مقدار config یا JSON را موقتاً تغییر می‌دهد:

```powershell
python ma_strategy.py --params-source best `
  --set leverage=3 --set trade_amount_percent=0.25 --set adx_filter=false
```

مقادیر `--set` در صورت امکان با قواعد JSON خوانده می‌شوند؛ مانند عدد، `true`، `false`، رشته و `null`. نام اشتباه قبل از شروع بک‌تست رد می‌شود.

### دستورهای کاربردی

```powershell
# تست سرعت بدون GUI و بدون فایل خروجی
python ma_strategy.py --start 2024-01-01 --end 2025-01-01 `
  --no-chart --no-trade-log --quiet --print-result

# ذخیره چارت بدون بازشدن پنجره
python ma_strategy.py --start 2025-01-01 --end 2025-04-01 `
  --no-chart --save-chart outputs/charts/q1_2025.png

# خروجی مستقل برای یک آزمایش
python ma_strategy.py --output-dir outputs/runs/conservative `
  --result-json outputs/runs/conservative/result.json `
  --set leverage=3 --set trade_amount_percent=0.25

# ساخت CSV، گزارش ماهانه و Excel پیش‌فرض بدون چارت
python ma_strategy.py --no-chart
```

### فایل‌های خروجی بک‌تست

در حالت عادی زیر مسیر `--output-dir` ساخته می‌شوند:

```text
outputs/
├── trades/data_orders.csv
├── trades/data_orders.xlsx       # پیش‌فرض؛ غیرفعال‌سازی با --no-excel
└── monthly/monthly_data_orders.csv
```

فایل XLSX دارای سربرگ ثابت، فیلتر، رنگ‌بندی سود و زیان و برگه‌های جداگانه Overview، All Trades، Main Strategy، RSI Strategy و Scale Strategy است. فایل CSV خام برای پردازش برنامه‌ای نیز حفظ می‌شود.

نتیجه نهایی شامل موجودی نهایی، سود کل، realized و unrealized، تعداد پوزیشن باز، درصد بازده، تعداد معاملات بسته، برد و باخت، win rate، maximum drawdown، score، profit factor، expectancy، Calmar ratio و آمار بخش‌های MA، RSI و Scale است.

### کنترل‌های چارت تعاملی

- چرخ ماوس: zoom با حفظ کندل زیر نشانگر.
- Drag با دکمه چپ: حرکت به گذشته یا آینده چارت.
- Drag با دکمه راست: تغییر مقیاس عمودی پنل انتخاب‌شده.
- دابل‌کلیک روی قیمت یا equity: تنظیم خودکار محور عمودی روی داده قابل‌مشاهده.
- Left یا `A`: کندل‌های قدیمی‌تر؛ Right یا `D`: کندل‌های جدیدتر.
- Up یا `W` یا Page Up: رفتن به قدیمی‌ترین پنجره.
- Down یا `S` یا Page Down: رفتن به جدیدترین پنجره.
- `0` یا Home: نمایش کل تاریخچه.
- `1` یا End: برگشت به پنجره اخیر پیش‌فرض.
- نگه‌داشتن ماوس نزدیک marker معامله: نمایش دلیل ورود یا خروج.

تنظیمات سرعت و تراکم چارت را می‌توان بدون ویرایش فایل تغییر داد:

```powershell
python ma_strategy.py `
  --set plot_max_candles=800 `
  --set plot_max_render_candles=600 `
  --set plot_drag_update_interval_ms=100
```

چارت اکنون آرایه‌ها و زمان‌ها را فقط یک بار تبدیل می‌کند، markerها را index می‌کند، در zoom دور OHLC را تجمیع می‌کند، هنگام drag پیش‌نمایش سبک‌تر می‌سازد و redraw نشانگر ماوس را محدود می‌کند. با zoom نزدیک جزئیات کامل کندل برمی‌گردد.

### استفاده داخل کد پایتون

```python
from ma_strategy import ma_strategy

result = ma_strategy(
    tune={"leverage": 3, "trade_amount_percent": 0.25},
    start="2025-01-01",
    end="2025-06-01",
    show_chart=False,
    write_trades=False,
    verbose=False,
)
print(result["total_profit_percent"])
```

## دفترچه راهنمای `optimize.py`

optimizer هنگام ارزیابی کاندیدها چارت، پیام‌های معامله و فایل‌نویسی معاملات را خاموش می‌کند. داده بازار و indicatorها نیز داخل هر worker cache می‌شوند.

### تفاوت Smart و Grid

- `smart`: تعداد `--tests` بودجه جستجو است. ابتدا ناحیه‌ها را می‌گردد، بهترین‌ها را نگه می‌دارد و به‌مرور اطراف گزینه‌های خوب mutation انجام می‌دهد. حالت پیشنهادی است.
- `grid`: تمام ترکیب‌های معتبر دکارتی profile را تست می‌کند. gridهای داخلی بسیار بزرگ‌اند؛ همیشه ابتدا `--dry-run` بزنید.

### تمام گزینه‌های خط فرمان optimizer

| گزینه | پیش‌فرض | کاربرد |
|---|---:|---|
| `-h`, `--help` | — | نمایش راهنمای داخلی. |
| `--mode smart\|grid` | `smart` | جستجوی هوشمند بودجه‌ای یا grid کامل. |
| `--tests N` | `5000` | بودجه کاندیدها در smart؛ محدودکننده grid نیست. |
| `--profile NAME` | وابسته به حالت | در Auto مقدار `full` و در حالت عادی `focused`؛ انتخاب‌های صریح: `focused`، `signal`، `exit`، `risk`، `rsi` یا `full`. |
| `--base-source config\|best\|file` | `config` | منبع پارامترهای خارج از profile انتخاب‌شده. |
| `--base-params FILE` | `outputs/optimize/best_params.json` | فایل JSON برای `best` یا `file`. |
| `-w N`, `--workers N` | حداکثر `8` | تعداد process؛ برای دیباگ ساده‌تر `1` بگذارید. |
| `--batch-size N` | `0` | اندازه batch و checkpoint؛ صفر یعنی خودکار. |
| `--chunksize N` | `0` | اندازه chunk پردازش چندپردازه؛ صفر یعنی خودکار. |
| `--elite-size N` | `20` | تعداد گزینه‌های برتر برای هدایت smart search. |
| `--seed N` | `42` | seed برای تکرارپذیری جستجوی smart. |
| `--start VALUE` | `2025-01-01` | شروع inclusive داده train. |
| `--end VALUE` | `2026-02-23` | پایان exclusive داده train. |
| `--validation-start VALUE` | — | شروع inclusive داده خارج از نمونه؛ همراه end لازم است. |
| `--validation-end VALUE` | — | پایان exclusive داده خارج از نمونه؛ همراه start لازم است. |
| `--validation-top N` | `20` | تعداد finalistهای train برای تست validation. |
| `--overfit-penalty X` | `0.25` | جریمه فاصله score آموزش و validation. |
| `--min-trades N` | `0` | حذف نتایج با معاملات بسته کمتر. |
| `--max-drawdown X` | — | حذف نتایج با drawdown مطلق بیشتر از این درصد. |
| `--output-dir DIR` | `outputs/optimize` | مسیر checkpointها و نتیجه‌ها. |
| `--resume` | خاموش | ادامه یک `optimization_results.csv` سازگار. |
| `--log-every N` | `10` | فاصله چاپ پیشرفت؛ صفر یعنی بدون گزارش پیشرفت. |
| `--top-n N` | `20` | تعداد گزینه‌های ذخیره‌شده در `top_results.json`. |
| `--excel-top N` | `5000` | تعداد بهترین candidateها در XLSX؛ مقدار `0` گزارش Excel را غیرفعال می‌کند. |
| `--list-profiles` | — | نمایش اندازه profileها و خروج. |
| `--dry-run` | — | نمایش اندازه برنامه جستجو بدون اجرای تست‌ها. |

```powershell
python optimize.py --help
python optimize.py --list-profiles
python optimize.py --profile risk --mode grid --dry-run
```

### کاربرد profileها

| Profile | کاربرد |
|---|---|
| `focused` | آستانه‌ها و وزن‌های امتیاز ورود و خروج؛ شروع عملی و سریع‌تر. |
| `signal` | شرط‌های ورود، indicatorها، filterها و وزن‌های ورود. |
| `exit` | شرط خروج، trailing، guardها و وزن‌های خروج. |
| `risk` | حجم معامله، اهرم، قوانین ماهانه، cooldown و scale-in. |
| `rsi` | زیر‌استراتژی RSI ماهانه داخل MA strategy. |
| `full` | همه پارامترها؛ فقط با بودجه smart منطقی است. |

بهینه‌سازی مرحله‌ای profileها معمولاً از جستجوی هم‌زمان همه متغیرها سریع‌تر، قابل‌فهم‌تر و قابل‌اعتمادتر است.

### روند پیشنهادی بهینه‌سازی

```powershell
# جستجوی focused از تنظیمات اصلی
python optimize.py --mode smart --profile focused --tests 5000 -w 8 `
  --start 2023-01-01 --end 2025-01-01 `
  --output-dir outputs/optimize/focused_01

# بهینه‌سازی خروج با حفظ سایر پارامترهای برنده قبلی
python optimize.py --mode smart --profile exit --tests 5000 -w 8 `
  --base-source file --base-params outputs/optimize/focused_01/best_params.json `
  --start 2023-01-01 --end 2025-01-01 `
  --output-dir outputs/optimize/exit_01

# اعتبارسنجی روی بازه جدا و جدیدتر
python optimize.py --mode smart --profile signal --tests 10000 -w 8 `
  --start 2023-01-01 --end 2025-01-01 `
  --validation-start 2025-01-01 --validation-end 2026-01-01 `
  --validation-top 30 --overfit-penalty 0.35 `
  --min-trades 50 --max-drawdown 35 `
  --output-dir outputs/optimize/signal_validated

# ادامه همان اجرای سازگار
python optimize.py --mode smart --profile signal --tests 10000 -w 8 `
  --output-dir outputs/optimize/signal_validated --resume

# اجرای پارامتر برنده
python ma_strategy.py --params-source best `
  --best-params outputs/optimize/signal_validated/best_params.json
```

برای resume باید profile، ستون‌های grid و مقادیر grid سازگار باشند. پس از تغییر profile یا ساختار grid از output directory جدید استفاده کنید.

### Score و جلوگیری از overfit

score از بازده، maximum drawdown، Calmar ratio، profit factor، expectancy، win rate، ثبات ماهانه، اطمینان تعداد معامله و جریمه liquidation تشکیل می‌شود. کاندید بدون معامله score بازنده می‌گیرد. `--min-trades` و `--max-drawdown` محدودیت سخت هستند.

در حالت validation، score مقاوم برابر است با score validation منهای `overfit_penalty × max(0, train_score - validation_score)`.

### فایل‌های خروجی optimizer

```text
optimization_results.csv     تمام کاندیدهای تکمیل‌شده و checkpoint
optimization_results.xlsx    برگه‌های رتبه‌بندی، پارامترها، معیارهای اصلی، RSI و Scale
best_params.json              برنده نهایی
optimization_summary.json     مشخصات اجرا و معیارهای برنده
top_results.json              تعداد --top-n از بهترین نتایج
best_training_params.json     برنده train در حالت validation
validation_results.json       جزئیات finalistهای خارج از نمونه
```

نوشتن JSON اتمیک است و CSV بعد از هر batch flush می‌شود تا ادامه اجرا با `--resume` قابل‌اعتمادتر باشد. تنظیمات پایه فقط یک‌بار به هر worker ارسال می‌شوند و هر task تنها تغییرات candidate را منتقل می‌کند. Smart Search علاوه بر exploration و crossover، همسایه‌های یک‌مرحله‌ای eliteها را به‌شکل deterministic بررسی می‌کند و ترکیب‌های مؤثر تکراری را کنار می‌گذارد.

### حالت پیوسته Auto

گزینه `--auto` یک کمپین قابل‌ادامه را تا زمان زدن `Ctrl+C` اجرا می‌کند. این حالت به‌صورت پیش‌فرض از grid اصلی `full` استفاده می‌کند؛ فقط برای جست‌وجوی عمداً کوچک‌تر `--profile focused` بدهید. هر چرخه دارای قیف پایداری با بازه‌های مستقل است: ۲۰۰۰ تست روی داده جدید، ۳۰ finalist برای validation، سپس ۱۰ مورد برای stress و در پایان ۳ مورد روی کل تاریخچه. بازه‌های پیش‌فرض عبارت‌اند از:

```text
Discovery    2025-01-01 -> آخرین کندل
Validation   2023-01-01 -> 2025-01-01
Stress       2019-01-01 -> 2023-01-01
Final        2019-01-01 -> آخرین کندل
```

```powershell
# شروع اجرای نامحدود؛ خروجی پیش‌فرض outputs/optimize/auto است
python .\optimize.py --auto -w 16

# برای توقف امن یک‌بار Ctrl+C بزنید

# ادامه دقیق همان چرخه و مرحله
python .\optimize.py --auto --resume -w 16

از نسخه جدید، اگر `auto_state.json` در پوشه خروجی موجود باشد همان دستور ساده
`--auto` نیز اجرای قبلی را خودکار ادامه می‌دهد و نوشتن `--resume` اجباری نیست.
Stateهای نسخه قبلی بدون حذف نتایج migrate می‌شوند؛ چرخه نیمه‌تمام با plan قبلی
کامل می‌شود و موتور جدید از چرخه بعدی فعال خواهد شد.

موتور Auto جدید ابتدا با Successive Halving کاندیداهای ضعیف را روی بازه‌های
ارزان‌تر حذف می‌کند، سپس برندگان را روی چند Fold زمانی مستقل Walk-forward
می‌سنجد. پس از جمع‌شدن حداقل داده لازم، مدل داخلی Extra Trees از تمام نتایج
Discovery کامل چرخه‌های قبل یاد می‌گیرد و ترکیبی از بهترین پیش‌بینی‌ها، نقاط
دارای عدم‌قطعیت و نمونه‌های تصادفی را برای چرخه بعد انتخاب می‌کند.

اگر کمپین Auto هنوز State نداشته باشد ولی `outputs/optimize/best_params.json`
موجود باشد، مقادیر سازگار آن به‌عنوان نقطه شروع استفاده می‌شوند.

برای مقایسه بازه‌های زمانی متفاوت، امتیاز خام مستقیماً استفاده نمی‌شود. مقدار
`objective_score` بدون تغییر نگه داشته می‌شود و مقدار جدید زیر نیز در CSV ثبت
می‌شود:

```text
time_normalized_score = objective_score × 35064 / range_candles
```

بنابراین امتیاز ۱۰۰ در بازه ۳۰روزه تقریباً معادل نرخ سالانه ۱۲۱۷٫۵ است و از
امتیاز ۲۰۰۰ در یک سال بهتر تشخیص داده نمی‌شود. رتبه‌بندی Walk-forward و مقایسه
Train/Validation از مقدار نرمال‌شده استفاده می‌کنند. CSVهای قدیمی هنگام Resume
به‌شکل اتمیک ارتقا می‌یابند و امتیاز خام یا نتایج قبلی حذف نمی‌شوند.

مسیر داغ Backtest نیز بدون تغییر قوانین معامله سبک‌تر شده است: بررسی‌های
Liquidation ناممکن قبل از ساخت State حذف می‌شوند، Positionها دسترسی مستقیم دارند
و محاسبات پنجره Cross cache می‌شوند. روی بازه پیش‌فرض، اجرای گرم یک تست حدود
۰٫۱۵ ثانیه شد و هر ۵۹ معیار برنده ذخیره‌شده بدون اختلاف باقی ماند.

# دیدن برنامه بدون اجرای بک‌تست
python .\optimize.py --auto --dry-run -w 16

# آزمایش محدود دوچرخه‌ای
python .\optimize.py --auto --auto-cycles 2 -w 16 `
  --output-dir outputs/optimize/auto_two_cycles
```

پس از چرخه اول، Auto می‌تواند مقادیر عددی خارج از grid اولیه بسازد. مثلاً اگر مقدار `20` از `[10, 20, 30]` برنده شود، مقادیری مانند `19` و `21` نیز تست می‌شوند. فاصله floatها در چرخه‌های بعد دقیق‌تر می‌شود. اعداد داخل مرز حداقل و حداکثر grid می‌مانند، boolean و enum همچنان گسسته هستند، روابط نامعتبر حذف می‌شوند و ترکیب‌های discovery قبلی تکرار نمی‌شوند.

Auto از نتایج Discovery یاد می‌گیرد کدام پارامترها اثر بیشتری دارند. تقریباً ۶۰٪ چرخه بعد جست‌وجوی محلی اطراف برندگان Hall of Fame، حدود ۲۵٪ exploration تصادفی و ۱۵٪ crossover است. پارامترهای اثرگذار mutation و تست همسایه بیشتری می‌گیرند، اما برای جلوگیری از قفل‌شدن زودهنگام همه متغیرها حداقل شانس جست‌وجو دارند. معیار امن پیش‌فرض `objective_score` است که سود و ریسک را با هم می‌سنجد؛ برای اولویت‌دادن عمدی به سود خام می‌توان `--auto-importance-target total_profit` را استفاده کرد.

هر چرخه والد خود را در `training_parent.json` ثبت می‌کند. پس از تکمیل چرخه اول، بهترین پارامتر Hall of Fame به baseline و والد mutation چرخه بعد تبدیل می‌شود؛ بنابراین train از بهترین مسیر شناخته‌شده ادامه پیدا می‌کند و هم‌زمان exploration تصادفی نیز حفظ می‌شود.

| گزینه Auto | پیش‌فرض | کاربرد |
|---|---:|---|
| `--auto` | خاموش | شروع کمپین پیوسته و مرحله‌ای. |
| `--auto-tests N` | `2000` | تعداد candidate جدید Discovery در هر چرخه. |
| `--auto-validation-top N` | `30` | تعداد برندگان Discovery برای Validation. |
| `--auto-stress-top N` | `10` | تعداد برندگان Validation برای Stress. |
| `--auto-final-top N` | `3` | تعداد برندگان Stress برای تست کل تاریخچه. |
| `--auto-hall-size N` | `20` | تعداد برندگان نگه‌داری‌شده بین چرخه‌ها. |
| `--auto-cycles N` | `0` | محدودیت چرخه؛ صفر یعنی ادامه تا `Ctrl+C`. |
| `--auto-discovery-start VALUE` | `2025-01-01` | شروع بازه جدید Discovery. |
| `--auto-validation-start VALUE` | `2023-01-01` | شروع Validation؛ پایان آن شروع Discovery است. |
| `--auto-stress-start VALUE` | `2019-01-01` | شروع Stress و تاریخچه کامل. |
| `--auto-end VALUE` | `latest` | پایان exclusive؛ مقدار latest آخرین کندل را پیدا می‌کند. |
| `--auto-importance-target METRIC` | `objective_score` | معیار اهمیت: امتیاز نهایی، سود یا درصد سود. |

نتیجه هر تست بلافاصله flush می‌شود و برای هر چرخه و مرحله checkpoint جدا وجود دارد. برای Resume باید profile، grid، پارامتر پایه، بازه‌ها، اندازه قیف و محدودیت‌های ریسک یکسان بمانند؛ تعداد worker و فاصله گزارش را می‌توان تغییر داد.

```text
auto_state.json              وضعیت دقیق کمپین، چرخه و مرحله
best_params.json             بهترین پارامتر مقاوم Hall of Fame
hall_of_fame.json/.csv       برندگان پایدار بین چرخه‌ها
parameter_importance.json/.csv اولویت یادگرفته‌شده پارامترها
auto_summary.json            خلاصه وضعیت و بهترین نتیجه
auto_report.xlsx             برگه‌های Hall of Fame و اهمیت پارامترها
cycles/cycle_*/              برنامه و CSV نتیجه هر مرحله
cycles/cycle_*/training_parent.json  پارامتر برنده‌ای که چرخه از آن ادامه یافته است
cycles/cycle_*/best_params.json      بهترین پارامتر آخرین مرحله همان چرخه
cycles/cycle_*/checkpoints/*/best_params.json  بهترین پارامتر در هر checkpoint
```

## اجرای تست‌ها

```powershell
python -m unittest discover -v
python -m py_compile ma_strategy.py optimize.py trade_engine.py chart_renderer.py
```

## رفع اشکال

- چارت کند است: `plot_max_candles` و `plot_max_render_candles` را کم کنید یا برای تحقیق `--no-chart` بزنید.
- ساخت گزارش کند است: با `--no-excel` گزارش قالب‌بندی‌شده را غیرفعال کنید.
- CSV/XLSX در Excel باز است: فایل را ببندید یا `--output-dir` دیگری انتخاب کنید.
- optimizer حافظه زیادی می‌گیرد: ابتدا `--workers` و سپس `--batch-size` را کاهش دهید.
- خطای multiprocessing ویندوز: با `-w 1` اجرا کنید تا خطای اصلی واضح شود.
- هیچ معامله‌ای انجام نشده: thresholdها، محدوده تاریخ، `--min-trades` و منبع پارامتر را بررسی کنید.
- نام پارامتر JSON یا `--set` اشتباه است: `python ma_strategy.py --list-params` را اجرا کنید.

## ساختار پروژه

```text
ma_strategy.py          استراتژی، CLI و جریان سیگنال
trade_engine.py         اجرا، حسابداری، liquidation و گزارش
trade_csv_logger.py     خروجی CSV و Excel اختیاری
chart_renderer.py       چارت تعاملی و قابل ذخیره
indicators.py           indicatorهای برداری
strategy_config.py      تنظیمات typed پیش‌فرض
optimize.py             optimizer هوشمند/grid و چندپردازه
check_monthly_data.py   سازنده گزارش ماهانه
data_candle/            داده ورودی
outputs/                خروجی‌های تولیدشده
```

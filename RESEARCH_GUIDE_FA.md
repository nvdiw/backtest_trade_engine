# راهنمای تحقیق معتبر و بهینه‌سازی چنداستراتژی

این راهنما مسیر پیشنهادی استفاده از optimizer جدید را نشان می‌دهد. هدف، پیدا کردن عددی با سود بیشتر روی گذشته نیست؛ هدف پیدا کردن ناحیه‌ای از پارامترهاست که در چند بازه‌ی زمانی، بعد از هزینه‌ها و بدون استفاده از اطلاعات آینده، رفتار باثبات‌تری داشته باشد.

> هیچ بک‌تستی سود آینده را تضمین نمی‌کند. گزینه‌ی `accepted=true` فقط یعنی نتیجه از gateهای آماری تعیین‌شده عبور کرده است.

## تقسیم امن داده

حالت پیش‌فرض `--date-policy auto` است و بازه‌ها را از آخرین کندل معتبر به عقب می‌سازد. با دیتاست فعلی:

- Candidate search و Top 100: از `2023-03-01` تا قبل از `2025-06-01`، با Discovery از `2024-03-01`
- Nested walk-forward: چهار OOS دوماهه از مرز Development تا `2026-02-01`
- Embargo: از `2026-02-01` تا `2026-03-01`
- Sealed Holdout: از `2026-03-01` تا آخرین کندل `2026-07-31 23:45:00` با پایان exclusive برابر `2026-08-01 00:00:00`

این تاریخ‌ها هنگام ساخت کمپین قفل می‌شوند. با اضافه‌شدن داده، کمپین جدید بازه‌ی تازه می‌گیرد اما Resume همان مرزهای قبلی را حفظ می‌کند. برای تاریخ دستی، `--date-policy fixed` بدهید.

### محاسبه و cache اندیکاتورها

در هر بازه، استراتژی فقط کندل‌های همان بازه به‌علاوه‌ی warmup لازم برای بزرگ‌ترین period موجود در pool را بارگذاری می‌کند؛ از ابتدای ۲۰۱۸ دوباره معامله یا امتیاز محاسبه نمی‌شود. کندل‌های warmup واقعاً برای seed کردن MA، RSI، ATR و ADX مصرف می‌شوند اما از معامله، سود و آمار حذف هستند. هر worker یک warmup ثابت دارد تا همه‌ی candidateها یک slice مشترک بگیرند؛ آرایه‌های MA/RSI و سایر اندیکاتورهای همسان نیز با کلید بازه و period cache می‌شوند و فقط هنگام تغییر واقعی period دوباره ساخته می‌شوند.

این جداسازی مهم است: snapshot پیش از اولین OOS تمام می‌شود، پس Top 100 آینده‌ی foldهای گزارشی را ندیده است. `manifest.json` بازه‌ی مصرف‌شده‌ی snapshot را ذخیره می‌کند و research آن را کنترل می‌کند. seed هم‌پوشان یا بدون provenance به‌صورت پیش‌فرض رد می‌شود. گزینه‌ی `--allow-research-seed-overlap` فقط اجرای تشخیصی را ممکن می‌کند و gate مربوط را `false` نگه می‌دارد؛ چنین اجرايی هرگز `accepted=true` نمی‌شود. اگر در حالت `--date-policy fixed` مرزها را جلو ببرید، مسئولیت حفظ همین جداسازی با شماست.

## مرحله ۱: جست‌وجوی توسعه و Top 100 هر ۵۰ cycle

برای MA:

```powershell
python optimize.py --auto --strategy ma --profile full `
  --auto-cycles 50 --snapshot-cycles 50 --snapshot-top 100 `
  -w 8 --output-dir outputs/optimize/ma_campaign_recent
```

برای ادامه‌ی همان کمپین:

```powershell
python optimize.py --auto --strategy ma --profile full `
  --auto-cycles 100 --snapshot-cycles 50 --snapshot-top 100 `
  -w 8 --output-dir outputs/optimize/ma_campaign_recent --resume
```

اگر `--auto-cycles 0` باشد، اجرا تا `Ctrl+C` ادامه دارد. در cycleهای ۵۰، ۱۰۰، ۱۵۰ و ... خروجی زیر ساخته می‌شود:

```text
outputs/optimize/ma_campaign_recent/snapshots/cycles_000050/
├── top_100.json
├── top_100.csv
├── best_params.json
├── manifest.json
└── params/
    ├── rank_001_params.json
    ├── rank_002_params.json
    └── ... rank_100_params.json
└── summaries/
    ├── rank_001_summary.json
    └── ... rank_100_summary.json
```

رتبه‌بندی snapshot فقط سود خام نیست؛ امتیاز robust چندبازه‌ای و پایداری ناحیه‌ی اطراف پارامتر نیز در آن لحاظ می‌شود.
فایل‌های `best_params.json` و `params/rank_*.json` فقط deltaهای grid نیستند؛ defaultهای مؤثر strategy نیز داخلشان expand و منجمد می‌شود تا تغییر بعدیِ کد/config نتیجه‌ی قدیمی را بی‌صدا عوض نکند.

## مرحله ۲: nested walk-forward واقعی

یکی از فایل‌های snapshot را به‌عنوان baseline منجمد کنید و انتخاب نهایی را فقط با Train و inner-validation انجام دهید. OOS هر fold فقط گزارش می‌شود و هیچ‌وقت به Hall of Fame، surrogate یا انتخاب fold بعدی برنمی‌گردد.

```powershell
python optimize.py --research --strategy ma --profile full `
  --base-source best `
  --base-params outputs/optimize/ma_campaign_recent/snapshots/cycles_000050/best_params.json `
  --research-seeds outputs/optimize/ma_campaign_recent/snapshots/cycles_000050/top_100.json `
  --wf-train-months 24 --wf-validation-months 3 `
  --wf-test-months 2 --wf-step-months 2 `
  --research-tests 500 --research-validation-top 50 `
  --research-pbo-candidates 20 --bootstrap-samples 1000 `
  --min-fold-trades 5 --min-total-oos-trades 30 `
  --max-oos-liquidations 0 `
  --min-oos-folds 4 --min-positive-fold-ratio 0.60 `
  --min-dsr-probability 0.95 --max-pbo 0.20 `
  --min-parameter-consensus 0.50 --max-parameter-spread 0.35 `
  -w 8 --output-dir outputs/optimize/ma_campaign_recent
```

فایل `--research-seeds` اختیاری است. وقتی داده شود، candidateهای سازگارِ Top 100 با همان ترتیب snapshot در ابتدای pool ثابت research قرار می‌گیرند و بقیه‌ی ظرفیت با Halton deterministic پر می‌شود. optimizer تأیید می‌کند که `development_end_exclusive` آن snapshot قبل از اولین OOS است. منشأ candidate پیشنهادی در `recommended_candidate_source` و وضعیت زمانی seed در `research_seed_provenance` ثبت می‌شود؛ نتیجه‌های OOS همچنان هیچ نقشی در انتخاب ندارند.

اگر اجرا قطع شد، دقیقاً همان فرمان را با `--resume` اجرا کنید. fingerprint داده، کد، foldها و کل candidate pool بررسی می‌شود؛ بنابراین checkpoint ناسازگار به‌اشتباه ادامه پیدا نمی‌کند.

خروجی‌های مهم:

- `walk_forward_research/walk_forward_report.json`: عملکرد OOS تجمیع‌شده، CAGR، Sharpe، Sortino، Calmar و gateها
- `walk_forward_research/walk_forward_candidate_params.json`: برنده‌ی انتخاب‌شده با Train/inner-validation برای بررسی، حتی اگر gateها رد شوند
- `walk_forward_research/walk_forward_recommended_params.json`: فقط در صورت `accepted=true` ساخته می‌شود و تنها مسیر پیش‌فرض مجاز برای holdout است
- `walk_forward_research/research_decision.json`: وضعیت آماده‌بودن holdout و نام gateهای ردشده
- `walk_forward_research/research_candidate_catalog.csv/json`: Candidateهای منتخب Train/Validation با فایل پارامتر مستقل، ابتدا اطلاعات تصمیم و سپس متغیرها
- `walk_forward_research/trial_ledger.json`: تعداد دقیق train، validation و OOS test و تعداد مؤثر trialهای DSR
- `walk_forward_research/parameter_stability.json`: تغییر هر پارامتر بین foldها، consensus و spread
- `walk_forward_research/fold_XX.json`: انتخاب و نتیجه‌ی کامل هر fold
- `market_data_audit.json` و `research_manifest.json`: audit داده و SHA-256 داده، کد و تنظیمات اجرا

اگر `accepted` برابر `false` است، نتیجه را به‌عنوان استراتژی تأییدشده تلقی نکنید؛ دلیل دقیق در `acceptance_gates` ثبت می‌شود.

## مرحله ۳: فقط یک بار sealed holdout

بعد از بررسی گزارش development، فایل پارامتر را تغییر ندهید و یک بار holdout را مصرف کنید:

```powershell
python optimize.py --sealed-holdout --strategy ma `
  --holdout-params outputs/optimize/ma_campaign_recent/walk_forward_research/walk_forward_recommended_params.json `
  --holdout-min-trades 20 --holdout-max-drawdown 30 `
  --output-dir outputs/optimize/ma_campaign_recent
```

برای MA و RSI سه سناریوی هزینه اجرا می‌شود:

- `base`: تنظیمات منجمدشده
- `adverse`: کارمزد، slippage، funding و margin محافظه‌کارانه‌تر
- `severe`: هزینه و شرایط اجرایی شدیدتر

خودِ search نیز دیگر اجرای بدون اصطکاک نیست: base شامل fee برابر `0.0005`، slippage برابر `0.0001` در هر fill، funding محافظه‌کارانه‌ی `0.00005` در هر ۸ ساعت، maintenance margin برابر `0.005` و liquidation fee برابر `0.002` است. funding ثابت فقط یک proxy محافظه‌کارانه است؛ اگر سری تاریخی funding واقعی دارید، بهتر است آن را در strategy plug-in خود مصرف کنید و همین مقادیر را در manifest نگه دارید.

`holdout_ledger.json` fingerprint بازه و داده را ذخیره می‌کند. اجرای دوباره‌ی همان بازه مسدود می‌شود. `--allow-holdout-repeat` فقط برای عیب‌یابی است و خروجی را صریحاً `contaminated_repeat` علامت می‌زند؛ چنین نتیجه‌ای دیگر unseen نیست.

هر سه سناریو باید حداقل معامله، drawdown، سود مثبت، صفر liquidation و lower bound مثبتِ moving-block bootstrap را پاس کنند. تکرار آلوده حتی با نتایج خوب همیشه `accepted=false` باقی می‌ماند.

## تعویض MA با RSI

استراتژی مستقل نمونه در `rsi_strategy.py` وجود دارد. برای استفاده از grid داخل همان فایل:

```powershell
python optimize.py --auto --strategy rsi --profile full `
  --auto-cycles 50 --snapshot-cycles 50 --snapshot-top 100 `
  -w 8 --output-dir outputs/optimize/rsi_campaign
```

برای استفاده از grid کوچک و قابل‌ویرایش JSON:

```powershell
python optimize.py --research --strategy rsi `
  --param-grid param_grids/rsi_quick.json --profile full `
  --research-seeds outputs/optimize/rsi_campaign/snapshots/cycles_000050/top_100.json `
  --research-tests 300 -w 8 `
  --output-dir outputs/optimize/rsi_campaign
```

بنابراین برای استراتژی موجود لازم نیست `optimize.py` را تغییر دهید؛ فقط این دو ورودی عوض می‌شوند:

```text
--strategy module:function
--param-grid path/to/grid.json
```

## قرارداد افزودن استراتژی جدید

حداقل فایل `my_strategy.py`:

```python
param_grid = {
    "period": [7, 14, 21],
    "threshold": [20, 30, 40],
}

def my_strategy(tune, start, end, research=False):
    # سیگنال candle i باید در open کندل i+1 اجرا شود.
    result = run_your_backtest(tune, start, end)
    # حداقل کلیدهای ضروری:
    # score, closed_trades, maximum_drawdown, total_profit_percent
    # در research بهتر است monthly_returns و trade_profits نیز برگردند.
    return result
```

اجرا:

```powershell
python optimize.py --research `
  --strategy my_strategy:my_strategy `
  --param-grid my_grid.json `
  --data-file path/to/market.csv
```

hookهای اختیاری قابل شناسایی:

- `PARAMETER_PROFILES`: چند grid نام‌گذاری‌شده مانند `focused` و `full`
- `build_strategy_config(tune)`: ساخت و اعتبارسنجی config
- `load_strategy_tune(path)`: خواندن JSON منجمد
- `required_indicator_warmup(config)`: تعداد candleهای warmup
- `is_valid_candidate(params)`: حذف ترکیب‌های نامعتبر قبل از backtest
- `canonicalize_candidate(params, baseline)`: یکی‌کردن پارامترهای بی‌اثر
- `preload_optimizer_data(...)`: cache داده در worker
- `DATA_FILE`: مسیر دیتاست اختصاصی Strategy برای اجرا، تاریخ‌ها، audit و fingerprint
- `TIMEFRAME`: فاصله‌ی کندل مانند `1m` یا `15m`؛ با فاصله‌ی واقعی timestampها تطبیق داده می‌شود
- `EXECUTION_SCENARIOS`: سناریوهای هزینه‌ی holdout

## کنترل‌های اعتبار که خودکار اعمال می‌شوند

- audit ساختار OHLCV، timestamp، duplicate، ترتیب زمانی، gap، حجم صفر و مقادیر نامعتبر
- fingerprint داده، کد و config برای تکرارپذیری
- اجرای causal با ورود/خروج سیگنالی در open کندل بعدی
- fee، slippage، funding، maintenance margin و liquidation fee
- train → purge → inner validation → purge → reporting-only OOS
- candidate pool تکرارپذیر و space-filling با seed ثابت
- حداقل معامله و حداکثر drawdown در هر پنجره
- moving-block bootstrap برای حفظ وابستگی زمانی
- Deflated Sharpe Ratio برای جریمه‌ی multiple testing
- CSCV/PBO برای سنجش احتمال overfit
- parameter plateau و پایداری پارامتر بین foldها
- gate حداقل consensus و حداکثر spread فقط روی پارامترهای mutable، نه singletonهای هزینه
- median/worst OOS fold، نسبت foldهای مثبت و drawdown تجمیع‌شده
- الزام foldهای واقعاً qualified و صفر liquidation در OOS با پیش‌فرض‌ها
- دفتر trialها و ممنوعیت feedback از OOS

## بررسی plan بدون اجرای backtest

```powershell
python optimize.py --research --strategy rsi --dry-run
python optimize.py --sealed-holdout --strategy rsi `
  --holdout-params frozen.json --dry-run
python optimize.py --strategy rsi --list-profiles
```

ابتدا این سه فرمان را اجرا کنید تا strategy، grid، foldها، بازه‌ی holdout و سناریوهای هزینه قبل از مصرف CPU یا داده‌ی sealed مشخص باشند.

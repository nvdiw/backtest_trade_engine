# راهنمای ساخت Strategy جدید

فایل `example_strategy.py` قالب اجرایی یک Strategy مستقل روی تایم‌فریم یک‌دقیقه است. این Strategy از `ma_strategy.py` ارث‌بری نمی‌کند و فقط از API عمومی داده، اجرا، حسابداری و Optimize استفاده می‌کند.

## شروع سریع

فایل کندل یک‌دقیقه‌ای را در این مسیر قرار دهید:

```text
data_candle/btc_1m_data.csv
```

ستون‌های ضروری:

```text
Open time,Close time,Open,High,Low,Close,Volume
```

فاصله میانه‌ی `Open time`ها باید با `TIMEFRAME = "1m"` منطبق باشد. gap واقعی قابل گزارش است، اما معرفی اشتباه فایل 15m به‌عنوان 1m متوقف می‌شود.

قبل از اجرای سنگین:

```powershell
python optimize.py --strategy example_strategy:example_strategy --list-profiles

python optimize.py --auto --strategy example_strategy:example_strategy `
  --profile full --dry-run `
  --output-dir outputs/optimize/example_1m_campaign
```

شروع ۵۰ Cycle و انتشار Top 100:

```powershell
python optimize.py --auto `
  --strategy example_strategy:example_strategy `
  --profile full `
  --auto-cycles 50 `
  --snapshot-cycles 50 `
  --snapshot-top 100 `
  -w 8 `
  --output-dir outputs/optimize/example_1m_campaign
```

ادامه تا Cycle 100:

```powershell
python optimize.py --auto --resume `
  --strategy example_strategy:example_strategy `
  --profile full `
  --auto-cycles 100 `
  -w 8 `
  --output-dir outputs/optimize/example_1m_campaign
```

## ساخت Strategy واقعی خودتان

1. از `example_strategy.py` یک کپی با نامی مانند `my_1m_strategy.py` بسازید.
2. `DATA_FILE` و `TIMEFRAME` را تنظیم کنید.
3. نام Config و تابع اصلی را تغییر دهید.
4. پارامترها و `PARAMETER_PROFILES` را تعریف کنید.
5. فقط بخش تولید Signal داخل حلقه را با منطق خودتان جایگزین کنید.
6. اجرای سفارش را در open کندل بعد نگه دارید تا look-ahead ایجاد نشود.

سپس اجرا کنید:

```powershell
python optimize.py --auto `
  --strategy my_1m_strategy:my_1m_strategy `
  --profile full `
  --auto-cycles 50 `
  -w 8 `
  --output-dir outputs/optimize/my_1m_campaign
```

## قرارداد فایل Strategy

موارد اصلی:

```python
DATA_FILE = Path(__file__).parent / "data_candle" / "btc_1m_data.csv"
TIMEFRAME = "1m"

PARAMETER_PROFILES = {
    "focused": {...},
    "full": {...},
}

def build_strategy_config(tune=None):
    ...

def required_indicator_warmup(config):
    ...

def is_valid_candidate(params):
    ...

def my_1m_strategy(tune, start, end, research=False, **kwargs):
    ...
```

Optimizer خودش این موارد را از ماژول Strategy پیدا می‌کند. برای تغییر Strategy نیازی به ویرایش `optimize.py` نیست.

### Staged اختیاری

اگر می‌خواهید گروه‌های پارامتر به‌ترتیب جداگانه Optimize شوند، Profileهای مربوط و برنامه‌ی مراحل را داخل Strategy معرفی کنید:

```python
PARAMETER_PROFILES = {
    "signal": SIGNAL_PARAM_GRID,
    "risk": RISK_PARAM_GRID,
    "full": FULL_PARAM_GRID,
}

STAGED_PHASES = (
    ("signal", "signal"),
    ("risk", "risk"),
)
```

Example همین دو فاز را آماده دارد. چون دو فاز دارد، مثلاً ۲۵ Cycle برای هر فاز یک Snapshot در Cycle 50 می‌سازد:

```powershell
python optimize.py --auto --staged `
  --strategy example_strategy:example_strategy `
  --stage-cycles 25 --snapshot-cycles 50 --snapshot-top 100 `
  --auto-cycles 50 -w 8 `
  --output-dir outputs/optimize/example_1m_staged
```

اگر `STAGED_PHASES` تعریف نشود، Normal Auto همچنان تمام Discovery، Validation، Stress، Walk-forward و Final را اجرا می‌کند.

## قابلیت‌های عمومی Engine در Example

قالب نمونه از این قابلیت‌ها استفاده می‌کند:

- بارگذاری دیتاست مخصوص همان Strategy و warmup اندیکاتورها
- اجرای causal؛ Signal کندل بسته‌شده در open کندل بعد اجرا می‌شود
- Long و Short
- اندازه معامله و Leverage
- Fee و Slippage
- Funding هشت‌ساعته
- Maintenance margin و Liquidation
- AccountState، Balance و Mark-to-market
- Drawdown، Win rate، Profit factor، Expectancy و Calmar
- Trade log و CSV/Excel در اجرای معمولی
- خروجی استاندارد لازم برای Auto، Research و Holdout
- `EXECUTION_SCENARIOS` برای Stress هزینه‌ها

`TradeEngine.finalize_account()` خروجی عمومی را تولید می‌کند و هیچ آمار MA، RSI یا Scale را از Strategy جدید درخواست نمی‌کند. اگر Metric اختصاصی دارید، آن را از طریق `extra_metrics` اضافه کنید.

## قواعد مهم اعتبار تست

- تصمیم کندل `i` نباید با قیمت open همان کندل اجرا شود؛ حداقل open کندل `i+1`.
- اندیکاتور warmup برای محاسبه استفاده می‌شود، ولی نباید وارد سود و آمار بازه شود.
- اگر TP و SL داخل یک کندل قابل لمس‌اند، ترتیب محافظه‌کارانه و ثابتی تعریف کنید.
- هزینه‌های اجرا را هنگام Discovery صفر نکنید.
- `Research` و `sealed Holdout` را پس از انتخاب Candidate اجرا کنید.
- هر Strategy و Timeframe باید `output-dir` مستقل داشته باشد؛ State دو Strategy را Resume نکنید.

## اجزای معماری

```text
market_data.py       دیتاست، Timeframe، Coverage، تاریخ به Index و Warmup
example_strategy.py  قالب قابل کپی و محل منطق Signal
trade_engine.py      اجرا، حسابداری، Fee، Funding، Liquidation و نتیجه عمومی
strategy_adapter.py  کشف Strategy، Grid، Config، Data و Timeframe
optimize.py          Auto، Walk-forward، Research، Holdout و Top 100
```

# فهرست مستندات

همهٔ دستورهای ترمینال را از ریشهٔ پروژه اجرا کنید، نه از داخل `docs` یا `tests`.

## شروع و اجرای روزمره

- [دستورهای مستقیم ترمینال و انتقال کمپین](COMMANDS_FA.md)
- [راهنمای فارسی پروژه](README_FA.md)
- [راهنمای انگلیسی پروژه](../README.md)
- [شروع سریع Optimize](OPTIMIZE_QUICKSTART_FA.md)
- [انتخاب فایل پارامتر و ارزیابی ثابت](FIXED_PARAMS_GUIDE_FA.md)
- [منابع سیستم و انتخاب تایم‌فریم](RUNTIME_FOCUS_GUIDE_FA.md)

## Pulse

- [بازپخش دقیق و انتقال تجربه](PULSE_REPLAY_TRANSFER_FA.md)
- [پارامترهای مستقل دو جهت و قابلیت‌های تصمیم‌گیری](PULSE_UPGRADES_FA.md)
- [قواعد و ساختار استراتژی](PULSE_GUIDE_FA.md)
- [بازه‌های Optimize و پیشینهٔ Phase A](PULSE_OPTIMIZE_FA.md)
- [کمپین طولانی و شبکهٔ 1m/15m](PULSE_200_FA.md)

## گزارش‌ها، تحقیق و توسعه

- [تنظیمات جهت‌دار MA](DIRECTIONAL_AUTO_GUIDE_FA.md)
- [گزارش‌های ماهانه](MONTHLY_REPORTS_FA.md)
- [خروجی‌های مشترک](UNIFIED_OUTPUTS_FA.md)
- [پروتکل تحقیق و holdout](RESEARCH_GUIDE_FA.md)
- [ساخت استراتژی جدید](STRATEGY_PLUGIN_GUIDE_FA.md)
- [پوشه‌های خروجی و مالکیت کمپین](STRATEGY_WORKSPACES_FA.md)

## ساختار فایل‌ها

| مسیر | کاربرد |
|---|---|
| ریشهٔ پروژه | موتور، استراتژی‌ها و ورودی‌های اصلی اجرا |
| `docs/` | راهنماها |
| `tests/` | تست‌های واحد و یکپارچه |
| `param_grids/` | شبکه‌های پارامتر |
| `data_candle/` | داده‌های بازار محلی |
| `outputs/` | نتایج، گزارش‌ها و checkpointها |
| `local_notes/` | یادداشت‌ها و نسخه‌های پشتیبان شخصی؛ خارج از Git |

اجرای همهٔ تست‌ها از ریشه:

```powershell
python -m unittest discover -s tests -t . -v
```

اجرای یک گروه مشخص:

```powershell
python -m unittest tests.test_pulse_strategy -v
```

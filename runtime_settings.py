"""Runtime-only resource and market selection, inherited by spawned workers."""

from functools import wraps
import os
from pathlib import Path
import sys

_KEYS = ('BTE_DATA_FILE', 'BTE_TIMEFRAME', 'BTE_PERFORMANCE')
_priority_applied = None


def market_selection(data_file=None, timeframe=None):
    return (os.environ.get('BTE_DATA_FILE') or data_file,
            os.environ.get('BTE_TIMEFRAME') or timeframe)


def worker_count(mode, cpu_count=None):
    cpus = max(1, cpu_count if cpu_count is not None else (os.cpu_count() or 1))
    return {'power_saving': max(1, cpus // 4),
            'normal': max(1, min(8, cpus // 2)),
            'boost': max(1, cpus - 1)}[mode]


def performance_name(value):
    value = value.strip().lower().replace('-', '_').replace(' ', '_')
    if value not in ('power_saving', 'normal', 'boost'):
        raise ValueError('performance must be power_saving, normal or boost')
    return value


def add_runtime_arguments(parser, *, data_file=True):
    parser.add_argument('--performance', type=performance_name, default='normal',
                        choices=('power_saving', 'normal', 'boost'),
                        help='resource policy only; does not reduce test count or precision')
    if data_file:
        parser.add_argument('--data-file', help='OHLCV CSV used for this run')
    parser.add_argument('--timeframe', default='auto',
                        help='auto detects candle spacing; e.g. 1m or 15m validates an explicit interval')


def add_chart_arguments(parser):
    parser.add_argument('--no-chart', action='store_true', help='disable chart display and automatic PNG (explicit --save-chart still saves)')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--show-chart', dest='show_chart', action='store_true', default=True,
                       help='display the interactive chart (default)')
    group.add_argument('--no-show-chart', dest='show_chart', action='store_false',
                       help='save the chart without opening a window')
    parser.add_argument('--save-chart', metavar='FILE', help='custom PNG/PDF/SVG path')


def chart_options(args):
    return dict(show_chart=args.show_chart and not args.no_chart,
                chart_file=args.save_chart or (str(Path(args.output_dir) / 'chart.png') if not args.no_chart else None))


def configure_runtime(args, argv=None):
    arguments = sys.argv[1:] if argv is None else list(argv)
    explicit_workers = any(a in ('-w', '--workers') or a.startswith('--workers=')
                           or (a.startswith('-w') and a[2:].isdigit()) for a in arguments)
    if hasattr(args, 'workers') and not explicit_workers:
        args.workers = worker_count(args.performance)
    if getattr(args, 'data_file', None):
        path = Path(args.data_file).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f'data file not found: {path}')
        args.data_file = str(path)
        os.environ['BTE_DATA_FILE'] = str(path)
    os.environ['BTE_TIMEFRAME'] = args.timeframe
    os.environ['BTE_PERFORMANCE'] = args.performance
    _clear_market_caches()
    apply_process_policy()


def apply_process_policy():
    """Use a lower Windows priority for power saving, never realtime/high."""
    global _priority_applied
    if 'BTE_PERFORMANCE' not in os.environ:
        return
    policy = (os.getpid(), os.environ.get('BTE_PERFORMANCE', 'normal'))
    if _priority_applied == policy:
        return
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        priority = 0x4000 if policy[1] == 'power_saving' else 0x20
        if not kernel.SetPriorityClass(kernel.GetCurrentProcess(), priority):
            raise OSError('Unable to set the requested process priority')
    _priority_applied = policy


def _clear_market_caches():
    # Imports stay lazy to avoid circular dependencies during worker startup.
    from market_data import clear_market_data_cache
    clear_market_data_cache()
    if 'trade_engine' in sys.modules:
        sys.modules['trade_engine'].TradeEngine.load_market_data.cache_clear()


def runtime_session(function):
    """Restore process settings when CLI main is embedded in tests or another app."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        previous = {key: os.environ.get(key) for key in _KEYS}
        optimizer = sys.modules.get('optimize')
        previous_market = {key: getattr(optimizer, key) for key in
                           ('_ACTIVE_MARKET_DATA_SOURCE', '_ACTIVE_CANDLES_PER_YEAR')
                           if optimizer is not None and hasattr(optimizer, key)}
        previous_priority = None
        if os.name == 'nt':
            import ctypes
            kernel = ctypes.windll.kernel32
            kernel.GetCurrentProcess.restype = ctypes.c_void_p
            kernel.GetPriorityClass.argtypes = (ctypes.c_void_p,)
            previous_priority = kernel.GetPriorityClass(kernel.GetCurrentProcess())
        try:
            return function(*args, **kwargs)
        finally:
            for key, value in previous_market.items():
                setattr(optimizer, key, value)
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            _clear_market_caches()
            global _priority_applied
            _priority_applied = None
            if previous_priority:
                kernel.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
                kernel.SetPriorityClass(kernel.GetCurrentProcess(), previous_priority)
    return wrapped

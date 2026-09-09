"""Resolve a campaign folder before strategy, data and date initialization."""
import json
from pathlib import Path


def restore_campaign(parser, args, arguments):
    explicit = {action.dest for action in parser._actions
                if any(token.split('=', 1)[0] in action.option_strings
                       for token in arguments)}
    target = args.resume if isinstance(args.resume, str) else args.output_dir
    directory = Path(target)
    if isinstance(args.resume, str) and not directory.is_dir():
        matches = sorted({p.parent.resolve() for name in
                          ('campaign_config.json', 'auto_state.json', 'staged_state.json', 'research_manifest.json')
                          for p in Path('outputs').rglob(name)
                          if p.parent.name == target})
        if len(matches) != 1:
            raise ValueError('Campaign folder not found or ambiguous: ' + target
                             + '; provide its full path. Matches: '
                             + ', '.join(map(str, matches)))
        directory = matches[0]
    def read(name):
        path = directory / name
        return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    saved = read('campaign_config.json') or read('research_manifest.json').get('resolved_config', {})
    state = read('auto_state.json')
    if saved.get('frozen_base_tune') is not None:
        args._resume_base_tune = saved['frozen_base_tune']
    if not args.resume and not state:
        return arguments
    if not saved and not state:
        if isinstance(args.resume, str):
            raise ValueError(f'No saved campaign settings in {directory}')
        return arguments
    # Runtime controls and bounded execution limits belong to this invocation.
    transient = {'resume', 'output_dir', 'dry_run', 'list_profiles', 'refresh_auto_report',
                 'workers', 'performance', 'auto_cycles'}
    for key, value in saved.items():
        if hasattr(args, key) and key not in explicit | transient and not key.startswith('_'):
            setattr(args, key, value)
    if state:
        args._resume_state = state
        config = state.get('config', {})
        for key in ('strategy', 'profile', 'data_file', 'timeframe'):
            if key not in explicit and config.get(key) is not None:
                setattr(args, key, config[key])
        args.auto = True
        # An embedded grid is sufficient even if the original grid file moved.
        args._resume_grid = config.get('parameter_grid')
        if 'param_grid' not in explicit:
            args.param_grid = None
    args.output_dir = str(directory)
    args.resume = True
    # Prevent later strategy defaults from replacing restored paths and dates.
    return list(arguments) + ['--output-dir'] + [
        option for action in parser._actions for option in action.option_strings[:1]
        if action.dest in saved and action.dest not in transient
    ]

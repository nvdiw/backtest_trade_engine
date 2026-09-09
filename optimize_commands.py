"""Small, strategy-neutral command surface over the existing optimizer engine."""
import json
from pathlib import Path


def campaign_folder(value):
    path = Path(value)
    if (path / 'auto_state.json').is_file():
        return path
    standard = Path('outputs') / value / 'optimize'
    if (standard / 'auto_state.json').is_file():
        return standard
    matches = [p.parent for p in Path('outputs').rglob('auto_state.json') if p.parent.name == value]
    if len(matches) != 1:
        raise ValueError('Campaign not found or ambiguous; provide its full folder path')
    return matches[0]


def expand_arguments(arguments):
    arguments = list(arguments)
    if arguments and arguments[0] == 'resume':
        if len(arguments) < 2:
            raise ValueError('Usage: python optimize.py resume FOLDER [--cycles N]')
        return ['--resume', arguments[1], *arguments[2:]]
    if arguments and not arguments[0].startswith('-'):
        return ['--strategy', arguments[0], '--auto', *arguments[1:]]
    return arguments


def report_command(arguments):
    if not arguments or arguments[0] not in ('status', 'report'):
        return False
    if len(arguments) != 2:
        raise ValueError('Usage: python optimize.py status|report FOLDER')
    directory = campaign_folder(arguments[1])
    state = json.loads((directory / 'auto_state.json').read_text(encoding='utf-8'))
    if arguments[0] == 'report':
        from strategy_workspace import claim_output
        claim_output(directory, state['config'].get('strategy', 'ma_strategy:ma_strategy'), 'optimize')
        from campaign_reporting import publish_campaign
        from optimize import _write_auto_reports
        publish_campaign(directory, state, rebuild=True)
        hall_path = directory / 'hall_of_fame.json'
        hall = json.loads(hall_path.read_text()) if hall_path.is_file() else []
        importance_path = directory / 'parameter_importance.json'
        importance = json.loads(importance_path.read_text()) if importance_path.is_file() else {}
        _write_auto_reports(directory, hall, importance, state, tuple(state['config']['parameter_grid']))
        # Restore missing empty-snapshot reports using only evidence available
        # at that snapshot boundary; do not rewrite its original manifest.
        from campaign_reporting import read_json, write_json
        import shutil
        evidence = read_json(directory / 'stage_evidence.json', {})
        for manifest_path in (directory / 'snapshots').glob('cycles_*/manifest.json'):
            manifest = read_json(manifest_path, {})
            report_path = manifest_path.parent / 'snapshot_report.xlsx'
            if manifest.get('saved_candidates') == 0 and not report_path.exists():
                boundary = int(manifest['cycle'])
                captured = {key: value for key, value in evidence.items() if value['cycle'] <= boundary}
                write_json(manifest_path.parent / 'stage_evidence.json', captured)
                snapshot_state = dict(state, status='snapshot', cycle=boundary,
                                      cycles_completed=boundary,
                                      total_evaluations=sum(item['evaluations'] for item in captured.values()))
                publish_campaign(manifest_path.parent, snapshot_state)
                shutil.copy2(manifest_path.parent / 'campaign_report.xlsx', report_path)
        print(f'Reports rebuilt from saved evaluations: {directory / "campaign_report.xlsx"}')
    print(f"{state.get('status')} | {state.get('cycles_completed', 0)} cycles completed | "
          f"{state.get('total_evaluations', 0):,} evaluations | "
          f"{state.get('config', {}).get('strategy')} | {state.get('config', {}).get('timeframe')}")
    path = directory / 'hall_of_fame.json'
    hall = json.loads(path.read_text()) if path.is_file() else []
    print(f'Qualified finalists: {len(hall)}')
    if not hall:
        print('No qualified final winner. Checkpoint parameters are provisional.')
    print(f'Output: {directory.resolve()}')
    return True

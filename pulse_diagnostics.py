"""Reporting-only measurements of executed sizing constraints."""


def sizing_metrics(diagnostics):
    result = {}
    for side in ('long', 'short'):
        count = diagnostics[side + '_filled_entries']
        result[side + '_filled_entries'] = count
        for metric, source in (('average_gross_exposure', 'gross_exposure_sum'),
                               ('average_margin_fraction', 'margin_fraction_sum')):
            result[side + '_' + metric] = diagnostics[side + '_' + source] / count if count else None
        for limit in ('risk', 'exposure', 'margin', 'cash'):
            result[side + '_sizing_' + limit + '_entries'] = diagnostics[side + '_sizing_' + limit]
    return result

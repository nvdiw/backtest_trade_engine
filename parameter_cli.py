"""Normalize the public parameter-file spelling without breaking legacy flags."""


def optimizer_parameter_arguments(arguments):
    arguments = list(arguments)
    has_file = any(token.split('=', 1)[0] == '--params-file' for token in arguments)
    has_source = any(token.split('=', 1)[0] in ('--params-source', '--base-source') for token in arguments)
    aliases = {'--params-file': '--base-params', '--params-source': '--base-source'}
    normalized = []
    for token in arguments:
        flag, separator, value = token.partition('=')
        normalized.append(aliases.get(flag, flag) + separator + value)
    if has_file and not has_source:
        normalized += ['--base-source', 'file']
    return normalized

import json
import tempfile
import unittest
from pathlib import Path

from parameter_cli import optimizer_parameter_arguments
from optimize import build_parser
from ma_strategy import resolve_parameter_source


class ParameterCliTests(unittest.TestCase):
    def test_file_alone_and_legacy_flags(self):
        for spelling in (['--params-file', 'a.json'], ['--params-file=a.json']):
            args = build_parser().parse_args(optimizer_parameter_arguments(spelling))
            self.assertEqual((args.base_source, args.base_params), ('file', 'a.json'))
        legacy = ['--base-source', 'best', '--base-params', 'old.json']
        self.assertEqual(optimizer_parameter_arguments(legacy), legacy)
        args = build_parser().parse_args(optimizer_parameter_arguments(
            ['--params-source', 'best', '--params-file', 'a.json']))
        self.assertEqual(args.base_source, 'best')

    def test_ma_file_alone_loads_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'params.json'
            path.write_text(json.dumps({'leverage': 3}))
            tune, source = resolve_parameter_source(params_file=path)
            self.assertEqual(tune['leverage'], 3)
            self.assertEqual(source, str(path))
        with self.assertRaises(FileNotFoundError):
            resolve_parameter_source(params_file='missing-parameter-cli-test.json')


if __name__ == '__main__':
    unittest.main()

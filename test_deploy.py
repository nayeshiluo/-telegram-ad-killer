"""Deployment contract: private configuration must work with DynamicUser."""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent


class DeploymentTests(unittest.TestCase):
    def test_service_loads_private_configuration_as_credential(self):
        unit = (ROOT / 'ad-killer.service').read_text()
        self.assertIn('DynamicUser=yes', unit)
        self.assertIn('LoadCredential=config.json:/etc/ad-killer/config.json', unit)
        self.assertIn('Environment=AD_CONFIG=%d/config.json', unit)
        self.assertIn('LoadCredential=bot-token:/etc/ad-killer/bot-token', unit)
        self.assertIn('LoadCredential=ai-key:/etc/ad-killer/ai-key', unit)

    def test_configuration_example_has_no_embedded_credentials(self):
        config = json.loads((ROOT / 'config.example.json').read_text())
        self.assertFalse(config['ai']['enabled'])
        self.assertEqual(config['ai']['model'], 'your-model')
        self.assertEqual(config['expected_username'], 'your_ad_killer_bot')
        self.assertFalse(any(k in config['ai'] for k in ('key', 'api_key', 'token')))
        self.assertTrue(all(p['mode'] == 'observe' for p in config['groups'].values()))

    def test_private_runtime_files_are_gitignored(self):
        rules = (ROOT / '.gitignore').read_text().splitlines()
        for entry in ('config.json', 'bot-token', 'ai-key'):
            self.assertIn(entry, rules)


if __name__ == '__main__':
    unittest.main()

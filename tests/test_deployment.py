"""Deployment preparation is local by default and excludes private workstation files."""
import hashlib
import json
import struct
import tarfile
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from scripts import build_lambda, publish_site


class RuntimeAssetTests(unittest.TestCase):
    def test_source_allowlist_excludes_local_settings_notebooks_and_static_ui(self):
        names = [str(path.relative_to(build_lambda.ROOT)) for path in build_lambda.sources()]
        self.assertIn('autosde/aws.py', names)
        self.assertIn('autosde/prompts/coordinator.md', names)
        self.assertTrue(all(name.endswith(('.py', '.md')) for name in names))
        self.assertNotIn('autosde/prompts/agents.json', names)
        self.assertFalse(any('static/' in name or '.env' in name for name in names))

    def test_scanner_extraction_checks_checksum_and_linux_architecture(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive, binary = Path(tmp) / 'scanner.tar.gz', Path(tmp) / 'bin/gitleaks'
            data = bytearray(32)
            data[:6] = b'\x7fELF\x02\x01'
            data[18:20] = struct.pack('<H', 62)
            with tarfile.open(archive, 'w:gz') as bundle:
                member = tarfile.TarInfo('gitleaks')
                member.size = len(data)
                bundle.addfile(member, BytesIO(data))
            with self.assertRaisesRegex(ValueError, 'checksum'):
                build_lambda.extract_gitleaks(archive, binary)
            with patch.object(build_lambda, 'GITLEAKS_ARCHIVE_SHA256', hashlib.sha256(archive.read_bytes()).hexdigest()):
                build_lambda.extract_gitleaks(archive, binary)
            self.assertEqual(binary.read_bytes(), data)
            self.assertTrue(binary.stat().st_mode & 0o111)

    def test_manifest_detects_modified_source_and_unexpected_packaged_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'autosde/aws.py'
            source.parent.mkdir()
            source.write_text('source')
            lock = root / 'requirements.lock'
            lock.write_text('dependencies')
            output = root / 'asset'
            output.mkdir()
            (output / 'file.py').write_text('bundle')
            manifest = {'sources': {'autosde/aws.py': build_lambda.sha256(source)},
                        'requirements_sha256': build_lambda.sha256(lock),
                        'files': {'file.py': build_lambda.sha256(output / 'file.py')}}
            (output / 'asset-manifest.json').write_text(json.dumps(manifest))
            with patch.object(build_lambda, 'ROOT', root), patch.object(build_lambda, 'LOCK', lock):
                build_lambda.verify(output)
                (output / '.env').write_text('private-sentinel')
                with self.assertRaisesRegex(ValueError, 'contents'):
                    build_lambda.verify(output)
                (output / '.env').unlink()
                source.write_text('changed')
                with self.assertRaisesRegex(ValueError, 'sources changed'):
                    build_lambda.verify(output)


class FrontendStagingTests(unittest.TestCase):
    def test_staging_replaces_local_config_without_publishing_or_changing_the_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'autosde/static'
            (source / 'assets').mkdir(parents=True)
            (source / 'assets/app.js').write_text('app')
            (source / 'index.html').write_text('frontend')
            (source / 'config.json').write_text('{"mode":"local"}')
            config = {'mode': 'cloud', 'apiBaseUrl': '', 'uploadOrigin': 'https://docs-bucket.s3.us-east-1.amazonaws.com', 'cognito': {'redirectUri': 'https://app.example.test/'}}
            outputs = root / 'outputs.json'
            outputs.write_text(json.dumps({'AutoSDELocal-dev': {'RuntimeConfig': json.dumps(config), 'SiteUrl': 'https://app.example.test/', 'DocumentBucket': 'docs-bucket', 'Region': 'us-east-1'}}))
            staging = root / '.build/site'
            with patch.object(publish_site, 'ROOT', root), patch.object(publish_site, 'STAGING', staging), patch('subprocess.run') as command:
                publish_site.stage_site(outputs, 'AutoSDELocal-dev')
                command.assert_not_called()
            self.assertEqual(json.loads((staging / 'config.json').read_text()), config)
            self.assertEqual(json.loads((source / 'config.json').read_text()), {'mode': 'local'})
            self.assertEqual((staging / 'assets/app.js').read_text(), 'app')

    def test_publish_refuses_wrong_account_before_uploading(self):
        with patch('subprocess.run') as command:
            command.return_value.stdout = json.dumps({'Account': '999999999999'})
            with self.assertRaisesRegex(ValueError, 'account/region'):
                publish_site.publish({'AccountId': '123456789012', 'Region': 'us-east-1'}, 'test', 'us-east-1')
            self.assertEqual(command.call_count, 1)

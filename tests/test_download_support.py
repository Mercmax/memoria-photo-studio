import io
import socket
import ssl
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
import download_support as support


class DownloadSupportTest(unittest.TestCase):
    def test_server_finds_certifi_in_separate_ai_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            bundle=root/'.ai-env/lib/python3.11/site-packages/certifi/cacert.pem'
            bundle.parent.mkdir(parents=True);bundle.write_text('fixture')
            with patch.object(support,'ROOT',root),patch('download_support.importlib.util.find_spec',return_value=None):
                self.assertIn(bundle,support.certificate_bundles())

    def test_extra_trust_preserves_tls_verification(self):
        with patch('download_support.certificate_bundles',return_value=[]):
            context=support.https_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)
        with patch('download_support.certificate_bundles',return_value=[Path('trusted.pem')]),patch('download_support.ssl.create_default_context') as create:
            support.https_context()
            create.return_value.load_verify_locations.assert_called_once_with(cafile='trusted.pem')

    def test_network_errors_are_actionable_and_hide_signed_query(self):
        url='https://huggingface.co/file?token=secret'
        cases=[(urllib.error.URLError(ssl.SSLCertVerificationError('CERTIFICATE_VERIFY_FAILED')),'setup-ai-mac.sh'),
               (urllib.error.URLError(socket.gaierror(-2,'not found')),'DNS'),
               (urllib.error.URLError(TimeoutError()),'время'),
               (urllib.error.HTTPError(url,403,'Forbidden',{},None),'HTTP 403'),
               (OSError(28,'disk full'),'места'),(PermissionError(),'прав')]
        for error,expected in cases:
            with self.subTest(expected=expected):
                message=support.download_error(error,url)
                self.assertIn(expected,message);self.assertNotIn('secret',message)
